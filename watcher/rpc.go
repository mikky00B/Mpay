package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
)

type watcher struct {
	cfg        config
	httpClient *http.Client
}

// rpcCall performs a JSON-RPC 2.0 call and returns the `result` member.
func (w *watcher) rpcCall(ctx context.Context, method string, params any) (json.RawMessage, error) {
	body, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 1, "method": method, "params": params,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, w.cfg.rpcURL, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := w.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	var envelope struct {
		Result json.RawMessage `json:"result"`
		Error  *struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return nil, fmt.Errorf("decode rpc response: %w", err)
	}
	if envelope.Error != nil {
		return nil, fmt.Errorf("rpc error %d: %s", envelope.Error.Code, envelope.Error.Message)
	}
	return envelope.Result, nil
}

// ingest POSTs a batch of events to the API. Non-2xx is an error so the
// caller retries; the API treats replays idempotently, so retrying is safe.
func (w *watcher) ingest(ctx context.Context, events []eventOut) error {
	if len(events) == 0 {
		return nil
	}
	body, _ := json.Marshal(map[string]any{"events": events})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, w.cfg.apiURL+"/internal/events", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Key", w.cfg.internalKey)
	resp, err := w.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		raw, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("ingest failed (%d): %s", resp.StatusCode, string(raw))
	}
	log.Printf("ingest: %d event(s) accepted", len(events))
	return nil
}

// ingestChunked POSTs events in batches of at most maxBatch — the API's
// IngestBatch limit is 500 and a single oversized POST used to 422 forever,
// wedging the watcher [D16]. The checkpoint is only advanced by the caller
// after ALL batches are accepted; a failed batch resends its whole window
// next tick, which the API absorbs idempotently [D7].
func (w *watcher) ingestChunked(ctx context.Context, events []eventOut) error {
	const maxBatch = 500
	for start := 0; start < len(events); start += maxBatch {
		end := start + maxBatch
		if end > len(events) {
			end = len(events)
		}
		if err := w.ingest(ctx, events[start:end]); err != nil {
			return err
		}
	}
	return nil
}
