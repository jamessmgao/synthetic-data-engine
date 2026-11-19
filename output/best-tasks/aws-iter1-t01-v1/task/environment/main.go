package main

import (
"fmt"
"log"
"net/http"
"os"
)

type TxRequest struct {
ID      int64
Key     string
Account string
Amount  int
Done    chan struct{}
}

type Transaction struct {
Key     string `json:"key"`
Account string `json:"account"`
Amount  int    `json:"amount"`
Status  string `json:"status"`
}

func main() {
os.MkdirAll("/app/data", 0755)
queue := make(chan TxRequest, 256)
ledger := NewLedger("/app/data")
cache := NewDedupCache()
ledger.Recover()
go ledger.ProcessLoop(queue)
h := &Handler{cache: cache, ledger: ledger, queue: queue}
mux := http.NewServeMux()
mux.HandleFunc("/tx", h.CreateTx)
mux.HandleFunc("/account/", h.GetAccount)
mux.HandleFunc("/status/", h.GetStatus)
fmt.Println("listening on :8080")
log.Fatal(http.ListenAndServe(":8080", mux))
}
