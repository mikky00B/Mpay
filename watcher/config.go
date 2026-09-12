package main

import (
	"encoding/json"
	"fmt"
	"os"
	"time"
)

type config struct {
	rpcURL        string
	apiURL        string
	internalKey   string
	hotAddress    string
	usdcContract  string
	confirmations uint64
	// maxBlocks caps the eth_getLogs block range per call [D16]: providers
	// reject large ranges (Alchemy ~10k, Infura tiers ~5k), which previously
	// wedged the watcher permanently after any long outage.
	maxBlocks     uint64
	pollInterval  time.Duration
	stateFile     string
}

func getEnvDefault(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func loadConfig() (config, error) {
	c := config{
		rpcURL:        os.Getenv("RPC_URL"),
		apiURL:        os.Getenv("API_URL"),
		internalKey:   os.Getenv("INTERNAL_API_KEY"),
		hotAddress:    os.Getenv("RECEIVING_ADDRESS"),
		usdcContract:  getEnvDefault("USDC_CONTRACT", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
		confirmations: 12,
		maxBlocks:     2000, // [D16] conservative vs. Alchemy 10k / Infura 5k caps
		pollInterval:  12 * time.Second,
		stateFile:     getEnvDefault("STATE_FILE", "state.json"),
	}
	if v := os.Getenv("CONFIRMATIONS"); v != "" {
		var n uint64
		if _, err := fmt.Sscanf(v, "%d", &n); err == nil && n > 0 {
			c.confirmations = n
		}
	}
	if v := os.Getenv("POLL_SECONDS"); v != "" {
		var s int
		if _, err := fmt.Sscanf(v, "%d", &s); err == nil && s > 0 {
			c.pollInterval = time.Duration(s) * time.Second
		}
	}
	if v := os.Getenv("MAX_BLOCKS"); v != "" {
		var n uint64
		if _, err := fmt.Sscanf(v, "%d", &n); err == nil && n > 0 {
			c.maxBlocks = n
		}
	}
	if c.rpcURL == "" || c.apiURL == "" || c.hotAddress == "" {
		return c, fmt.Errorf("RPC_URL, API_URL and RECEIVING_ADDRESS are required")
	}
	return c, nil
}

// checkpoint persists the last fully-processed block; resuming skips history.
type checkpoint struct {
	LastBlock uint64 `json:"last_block"`
}

func loadCheckpoint(path string) (uint64, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	var cp checkpoint
	if err := json.Unmarshal(b, &cp); err != nil {
		return 0, err
	}
	return cp.LastBlock, nil
}

// saveCheckpoint writes atomically (tmp file + rename) so a crash never
// leaves a half-written checkpoint behind.
func saveCheckpoint(path string, block uint64) error {
	b, _ := json.Marshal(checkpoint{LastBlock: block})
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
