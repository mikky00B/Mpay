package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strings"
	"time"
)

// run is the main loop: discover height, backfill unseen blocks (staying N
// confirmations behind head), ingest transfers, persist checkpoint.
// One failed poll never kills the watcher — the checkpoint only advances
// after a successful ingest, so crashes/retries are always safe.
func (w *watcher) run(ctx context.Context) error {
	last, err := loadCheckpoint(w.cfg.stateFile)
	if os.IsNotExist(err) {
		h, err := w.currentHeight(ctx)
		if err != nil {
			return err
		}
		last = h // first run: start at "now", don't rescan history
		if err := saveCheckpoint(w.cfg.stateFile, last); err != nil {
			return err
		}
		log.Printf("first run: starting from head %d", last)
	} else if err != nil {
		return err
	}

	ticker := time.NewTicker(w.cfg.pollInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			height, err := w.currentHeight(ctx)
			if err != nil {
				log.Printf("height poll failed: %v", err)
				continue
			}
			target := height
			if w.cfg.confirmations > 0 && height > w.cfg.confirmations {
				target = height - w.cfg.confirmations
			}
			if target > last {
				// [D16] Chunked scanning: real providers cap eth_getLogs
				// ranges (~10k Alchemy, ~5k Infura tiers). A single unbounded
				// call wedges the watcher forever after a long outage — the
				// failing range only grows and the checkpoint never advances.
				// Windows of maxBlocks keep every call legal; the checkpoint
				// advances only at window boundaries AFTER all of a window's
				// batches are accepted. A failed window just means the rest is
				// retried next tick with the progress so far kept.
				for from := last + 1; from <= target; from += w.cfg.maxBlocks {
					to := from + w.cfg.maxBlocks - 1
					if to > target {
						to = target
					}
					logs, err := w.filterLogs(ctx, from, to)
					if err != nil {
						log.Printf("getLogs failed (%d-%d): %v", from, to, err)
						break // keep progress made so far; retry the rest next tick
					}
				events := make([]eventOut, 0, len(logs))
				for _, l := range logs {
					amount, err := decodeAmount(l.Data)
					if err != nil {
						log.Printf("skip log %s:? : %v", l.TxHash, err)
						continue
					}
					logIndex, err := parseQuantity(l.Index)
					if err != nil {
						log.Printf("skip log %s: bad logIndex: %v", l.TxHash, err)
						continue
					}
					if len(l.Topics) < 3 {
						continue
					}
					events = append(events, eventOut{
						TxHash:          strings.ToLower(l.TxHash),
						LogIndex:        int(logIndex),
						BlockNumber:     int64(mustHexToUint64(l.BlockNumber)),
							BlockHash:       l.BlockHash,
							ContractAddress: strings.ToLower(l.Address),
							FromAddress:     "0x" + trimTopic(l.Topics[1]),
							ToAddress:       strings.ToLower(w.cfg.hotAddress),
							AmountBaseUnits: amount,
							Token:           "USDC",
						})
					}
					// [D16] Chunked ingest: the API rejects batches > 500 with
					// a 422; splitting keeps every POST legal. The checkpoint
					// advances only after ALL batches of the window are
					// accepted — partial failure means the window is rescanned
					// next tick, and replays are absorbed idempotently [D7].
					if err := w.ingestChunked(ctx, events); err != nil {
						log.Printf("ingest failed (%d-%d): %v", from, to, err)
						break // keep progress; retry this window next tick
					}
					log.Printf("blocks %d-%d: %d transfer(s) ingested", from, to, len(events))
					last = to
					if err := saveCheckpoint(w.cfg.stateFile, last); err != nil {
						log.Printf("checkpoint save failed: %v", err)
					}
				}
			}
		}
	}
}

// currentHeight returns the chain head; a malformed RPC response is an ERROR,
// never silently height 0 [D16].
func (w *watcher) currentHeight(ctx context.Context) (uint64, error) {
	raw, err := w.rpcCall(ctx, "eth_blockNumber", []any{})
	if err != nil {
		return 0, err
	}
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		return 0, fmt.Errorf("malformed eth_blockNumber result %q: %w", raw, err)
	}
	h, err := hexToUint64(s)
	if err != nil {
		return 0, fmt.Errorf("malformed eth_blockNumber quantity %q: %w", s, err)
	}
	return h, nil
}
