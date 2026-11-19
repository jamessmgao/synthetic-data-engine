package main

import (
"database/sql"
"path/filepath"

_ "github.com/mattn/go-sqlite3"
)

type Ledger struct {
db *sql.DB
}

func NewLedger(dataDir string) *Ledger {
db, err := sql.Open("sqlite3", filepath.Join(dataDir, "ledger.db")+"?_journal_mode=WAL")
if err != nil {
panic(err)
}
db.Exec(`CREATE TABLE IF NOT EXISTS transactions (
id INTEGER PRIMARY KEY AUTOINCREMENT,
key TEXT NOT NULL,
account TEXT NOT NULL,
amount INTEGER NOT NULL,
status TEXT NOT NULL DEFAULT 'pending'
)`)
db.Exec(`CREATE TABLE IF NOT EXISTS accounts (
name TEXT PRIMARY KEY,
balance INTEGER NOT NULL DEFAULT 0
)`)
return &Ledger{db: db}
}

func (l *Ledger) Recover() {
rows, err := l.db.Query("SELECT id, key, account, amount FROM transactions WHERE status = 'pending'")
if err != nil {
return
}
defer rows.Close()
for rows.Next() {
var id int64
var key, account string
var amount int
if err := rows.Scan(&id, &key, &account, &amount); err != nil {
continue
}
l.processOne(TxRequest{ID: id, Key: key, Account: account, Amount: amount, Done: nil})
}
}

func (l *Ledger) CreatePending(key, account string, amount int) int64 {
res, err := l.db.Exec("INSERT INTO transactions(key, account, amount, status) VALUES(?, ?, ?, 'pending')", key, account, amount)
if err != nil {
panic(err)
}
id, _ := res.LastInsertId()
return id
}

func (l *Ledger) ProcessLoop(ch <-chan TxRequest) {
for req := range ch {
l.processOne(req)
}
}

func (l *Ledger) processOne(req TxRequest) {
var status string
l.db.QueryRow("SELECT status FROM transactions WHERE id = ?", req.ID).Scan(&status)
if status != "pending" {
if req.Done != nil {
req.Done <- struct{}{}
}
return
}
_, err := l.db.Exec("INSERT INTO accounts(name, balance) VALUES(?, -?) ON CONFLICT(name) DO UPDATE SET balance = balance - ?", req.Account, req.Amount, req.Amount)
if err != nil {
panic(err)
}
l.db.Exec("UPDATE transactions SET status = 'completed' WHERE id = ?", req.ID)
if req.Done != nil {
req.Done <- struct{}{}
}
}

func (l *Ledger) GetTransaction(key string) *Transaction {
var tx Transaction
err := l.db.QueryRow("SELECT key, account, amount, status FROM transactions WHERE key = ? ORDER BY id DESC LIMIT 1", key).Scan(&tx.Key, &tx.Account, &tx.Amount, &tx.Status)
if err != nil {
return nil
}
return &tx
}

func (l *Ledger) GetAccount(name string) (int, bool) {
var balance int
err := l.db.QueryRow("SELECT balance FROM accounts WHERE name = ?", name).Scan(&balance)
if err != nil {
return 0, false
}
return balance, true
}
