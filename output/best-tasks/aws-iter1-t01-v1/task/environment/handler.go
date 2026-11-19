package main

import (
"encoding/json"
"net/http"
"sync"
)

type DedupCache struct {
mu    sync.RWMutex
items map[string]string
}

func NewDedupCache() *DedupCache {
return &DedupCache{items: make(map[string]string)}
}

func (c *DedupCache) Get(key string) (string, bool) {
c.mu.RLock()
defer c.mu.RUnlock()
s, ok := c.items[key]
return s, ok
}

func (c *DedupCache) Set(key, status string) {
c.mu.Lock()
defer c.mu.Unlock()
c.items[key] = status
}

type Handler struct {
cache  *DedupCache
ledger *Ledger
queue  chan<- TxRequest
}

func (h *Handler) CreateTx(w http.ResponseWriter, r *http.Request) {
if r.Method != http.MethodPost {
http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
return
}
var req struct {
Key     string `json:"key"`
Account string `json:"account"`
Amount  int    `json:"amount"`
}
if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
http.Error(w, "bad request", http.StatusBadRequest)
return
}
if status, ok := h.cache.Get(req.Key); ok {
w.Header().Set("Content-Type", "application/json")
json.NewEncoder(w).Encode(map[string]interface{}{"key": req.Key, "status": status})
return
}
id := h.ledger.CreatePending(req.Key, req.Account, req.Amount)
done := make(chan struct{})
h.queue <- TxRequest{ID: id, Key: req.Key, Account: req.Account, Amount: req.Amount, Done: done}
<-done
h.cache.Set(req.Key, "completed")
w.Header().Set("Content-Type", "application/json")
json.NewEncoder(w).Encode(map[string]interface{}{"key": req.Key, "status": "completed"})
}

func (h *Handler) GetAccount(w http.ResponseWriter, r *http.Request) {
name := r.URL.Path[len("/account/"):]
balance, ok := h.ledger.GetAccount(name)
if !ok {
http.Error(w, "not found", http.StatusNotFound)
return
}
w.Header().Set("Content-Type", "application/json")
json.NewEncoder(w).Encode(map[string]interface{}{"account": name, "balance": balance})
}

func (h *Handler) GetStatus(w http.ResponseWriter, r *http.Request) {
key := r.URL.Path[len("/status/"):]
tx := h.ledger.GetTransaction(key)
if tx == nil {
http.Error(w, "not found", http.StatusNotFound)
return
}
w.Header().Set("Content-Type", "application/json")
json.NewEncoder(w).Encode(tx)
}
