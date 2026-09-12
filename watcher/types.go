package main

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math/big"
	"strconv"
	"strings"
)

// Well-known event signatures (keccak256 of the canonical signature).
const transferTopic0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

// rpcLog is one entry from eth_getLogs.
//
// Numeric fields are hex-quantity STRINGS per the JSON-RPC spec ("0x0"), not
// JSON numbers — real providers (Alchemy/Infura) send strings, so they are
// decoded as strings and parsed explicitly (see parseQuantity).
type rpcLog struct {
	BlockNumber string   `json:"blockNumber"`
	BlockHash   string   `json:"blockHash"`
	TxHash      string   `json:"transactionHash"`
	Index       string   `json:"logIndex"`
	Address     string   `json:"address"`
	Topics      []string `json:"topics"`
	Data        string   `json:"data"`
}

// parseQuantity accepts a JSON-RPC quantity ("0x10d4f") or a plain decimal
// ("0"), covering spec-compliant providers and test mocks alike.
func parseQuantity(s string) (uint64, error) {
	if strings.HasPrefix(s, "0x") || strings.HasPrefix(s, "0X") {
		return hexToUint64(s)
	}
	if s == "" {
		return 0, fmt.Errorf("empty quantity")
	}
	v, err := strconv.ParseUint(s, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("bad quantity %q: %w", s, err)
	}
	return v, nil
}

// eventOut is the payload the watcher POSTs to the API ingest endpoint.
type eventOut struct {
	TxHash          string `json:"tx_hash"`
	LogIndex        int    `json:"log_index"`
	BlockNumber     int64  `json:"block_number"`
	BlockHash       string `json:"block_hash"`
	ContractAddress string `json:"contract_address"`
	FromAddress     string `json:"from_address"`
	ToAddress       string `json:"to_address"`
	AmountBaseUnits int64  `json:"amount_base_units"`
	Token           string `json:"token"`
}

// toTopic pads an EVM address to a 32-byte topic hex string.
func toTopic(addr string) (string, error) {
	h := strings.TrimPrefix(strings.ToLower(addr), "0x")
	b, err := hex.DecodeString(h)
	if err != nil || len(b) != 20 {
		return "", fmt.Errorf("bad address %q", addr)
	}
	return "0x" + strings.Repeat("0", 24) + h, nil
}

// decodeAmount parses the uint256 log data into int64 base units.
// USDC amounts fit int64 for any realistic payment (< 9.2e12 USDC).
func decodeAmount(data string) (int64, error) {
	h := strings.TrimPrefix(data, "0x")
	if h == "" {
		return 0, fmt.Errorf("empty transfer amount")
	}
	b, err := hex.DecodeString(h)
	if err != nil {
		return 0, fmt.Errorf("bad data hex: %w", err)
	}
	n := new(big.Int).SetBytes(b)
	if !n.IsInt64() {
		return 0, fmt.Errorf("amount overflows int64: %s", n.String())
	}
	return n.Int64(), nil
}

// filterLogs fetches ERC-20 Transfers TO the hot address in [from, to].
func (w *watcher) filterLogs(ctx context.Context, from, to uint64) ([]rpcLog, error) {
	toTopic, err := toTopic(w.cfg.hotAddress)
	if err != nil {
		return nil, err
	}
	params := map[string]any{
		"address": w.cfg.usdcContract,
		"topics":  []any{transferTopic0, nil, toTopic},
		"fromBlock": "0x" + fmt.Sprintf("%x", from),
		"toBlock":   "0x" + fmt.Sprintf("%x", to),
	}
	resp, err := w.rpcCall(ctx, "eth_getLogs", []any{params})
	if err != nil {
		return nil, err
	}
	var logs []rpcLog
	raw, _ := json.Marshal(resp)
	if err := json.Unmarshal(raw, &logs); err != nil {
		return nil, fmt.Errorf("parse logs: %w", err)
	}
	return logs, nil
}
