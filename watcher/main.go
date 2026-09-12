package main

import (
	"context"
	"log"
	"net/http"
	"time"
)

func main() {
	cfg, err := loadConfig()
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	w := &watcher{cfg: cfg, httpClient: &http.Client{Timeout: 30 * time.Second}}
	log.Printf("mpay watcher: rpc=%s api=%s hot=%s confirmations=%d poll=%s",
		cfg.rpcURL, cfg.apiURL, cfg.hotAddress, cfg.confirmations, cfg.pollInterval)
	if err := w.run(context.Background()); err != nil && err != context.Canceled {
		log.Fatalf("watcher exited: %v", err)
	}
}
