package main

import (
	"log/slog"
	"net/http"
	"os"

	"github.com/veil/veil/internal/httpapi"
	"github.com/veil/veil/internal/seed"
)

func main() {
	svc, err := seed.Development()
	if err != nil {
		slog.Error("loading development seed", "error", err)
		os.Exit(1)
	}
	slog.Info("starting development-only Veil API", "addr", ":8080")
	if err := http.ListenAndServe(":8080", httpapi.New(svc)); err != nil {
		slog.Error("server stopped", "error", err)
	}
}
