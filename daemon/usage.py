"""Usage, Telemetry, and Computing Resource Tracker.

Tracks:
- Outgoing and incoming Telegram message volume (per persona, per group, over time).
- AI completions, prompt tokens, completion tokens, response latency, and cost estimates.
- Active Telethon network sockets (1 Scout socket vs dormant zero-socket accounts).
- Generates timeseries analytics and persona speech distribution for dashboard graphs.
"""

import time
import logging

log = logging.getLogger("usage")


class UsageTracker:
    def __init__(self, store):
        self.store = store
        self.ensure_tables()

    def ensure_tables(self):
        with self.store.lock:
            cur = self.store.db.cursor()
            cur.execute("""
            CREATE TABLE IF NOT EXISTS usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                persona_id INTEGER,
                account_id INTEGER,
                chat_id INTEGER,
                tokens_prompt INTEGER DEFAULT 0,
                tokens_completion INTEGER DEFAULT 0,
                tokens_total INTEGER DEFAULT 0,
                latency_ms INTEGER DEFAULT 0,
                model TEXT DEFAULT '',
                cost_est REAL DEFAULT 0.0
            )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_logs(timestamp)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_pid ON usage_logs(persona_id, timestamp)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_event ON usage_logs(event_type, timestamp)")
            self.store.db.commit()

    def log_ai(self, event_type, persona_id=None, account_id=None, chat_id=None,
               tokens_prompt=0, tokens_completion=0, latency_ms=0, model=""):
        now_ts = int(time.time())
        p_tok = max(0, int(tokens_prompt or 0))
        c_tok = max(0, int(tokens_completion or 0))
        total_tok = p_tok + c_tok

        # Rough blended rate estimate: ~$0.0003 per 1K tokens
        cost_est = (total_tok / 1000.0) * 0.0003

        with self.store.lock:
            cur = self.store.db.cursor()
            cur.execute("""
                INSERT INTO usage_logs(
                    timestamp, event_type, persona_id, account_id, chat_id,
                    tokens_prompt, tokens_completion, tokens_total, latency_ms, model, cost_est
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (now_ts, event_type, persona_id, account_id, chat_id,
                  p_tok, c_tok, total_tok, int(latency_ms or 0), str(model or ""), cost_est))
            self.store.db.commit()

    def log_event(self, event_type, persona_id=None, account_id=None, chat_id=None):
        now_ts = int(time.time())
        with self.store.lock:
            cur = self.store.db.cursor()
            cur.execute("""
                INSERT INTO usage_logs(
                    timestamp, event_type, persona_id, account_id, chat_id
                ) VALUES(?,?,?,?,?)
            """, (now_ts, event_type, persona_id, account_id, chat_id))
            self.store.db.commit()

    def get_summary(self):
        now_ts = int(time.time())
        day_ago = now_ts - 86400
        week_ago = now_ts - (7 * 86400)

        # Message counts from usage_logs and messages table
        sent_today = len(self.store.rows("SELECT id FROM usage_logs WHERE event_type='msg_sent' AND timestamp >= ?", (day_ago,)))
        sent_7d = len(self.store.rows("SELECT id FROM usage_logs WHERE event_type='msg_sent' AND timestamp >= ?", (week_ago,)))
        sent_total = len(self.store.rows("SELECT id FROM usage_logs WHERE event_type='msg_sent'"))

        # Fallback to daily_batch_items and direct_messages if logs are fresh
        if sent_total == 0:
            batch_sent = len(self.store.rows("SELECT id FROM daily_batch_items WHERE status='sent'"))
            dm_sent = len(self.store.rows("SELECT id FROM direct_messages WHERE incoming=0"))
            sent_total = batch_sent + dm_sent
            sent_7d = sent_total
            sent_today = sent_total

        recv_today = len(self.store.rows("SELECT id FROM usage_logs WHERE event_type='msg_recv' AND timestamp >= ?", (day_ago,)))
        recv_total = len(self.store.rows("SELECT msg_id FROM messages WHERE is_bot=0"))

        # AI Tokens
        token_rows = self.store.rows("""
            SELECT 
                SUM(tokens_prompt) as prompt,
                SUM(tokens_completion) as comp,
                SUM(tokens_total) as total,
                AVG(CASE WHEN latency_ms > 0 THEN latency_ms ELSE NULL END) as avg_latency,
                SUM(cost_est) as total_cost
            FROM usage_logs 
            WHERE event_type IN ('ai_chat', 'ai_batch')
        """)
        tok = token_rows[0] if token_rows else {}
        total_tokens = tok.get("total") or 0
        prompt_tokens = tok.get("prompt") or 0
        comp_tokens = tok.get("comp") or 0
        avg_latency = round(tok.get("avg_latency") or 0)
        total_cost = round(tok.get("total_cost") or 0.0, 4)

        # Active accounts and sockets
        all_accs = self.store.rows("SELECT id, active FROM accounts")
        active_count = len([a for a in all_accs if a.get("active")])
        total_accs = len(all_accs)
        # In Scout architecture, only 1 persistent socket is maintained
        active_sockets = 1 if active_count > 0 else 0
        dormant_accounts = max(0, total_accs - active_sockets)

        return {
            "messages": {
                "sent_today": sent_today,
                "sent_7d": sent_7d,
                "sent_total": sent_total,
                "received_today": recv_today,
                "received_total": recv_total
            },
            "ai": {
                "total_tokens": total_tokens,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": comp_tokens,
                "avg_latency_ms": avg_latency,
                "cost_estimate_usd": total_cost
            },
            "compute": {
                "active_sockets": active_sockets,
                "dormant_accounts": dormant_accounts,
                "total_accounts": total_accs,
                "socket_overhead_percent": round((active_sockets / max(1, total_accs)) * 100, 1),
                "ram_savings_percent": round((dormant_accounts / max(1, total_accs)) * 100, 1)
            }
        }

    def get_timeseries(self, days=7):
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        result = []

        for i in range(days - 1, -1, -1):
            day_dt = now - datetime.timedelta(days=i)
            day_str = day_dt.strftime("%Y-%m-%d")
            label = day_dt.strftime("%b %d")
            day_start = int(datetime.datetime(day_dt.year, day_dt.month, day_dt.day, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
            day_end = day_start + 86400

            sent = len(self.store.rows(
                "SELECT id FROM usage_logs WHERE event_type='msg_sent' AND timestamp >= ? AND timestamp < ?",
                (day_start, day_end)
            ))
            # Also check daily batch items for historical context
            batch_sent = len(self.store.rows(
                "SELECT id FROM daily_batch_items WHERE status='sent' AND sent_ts >= ? AND sent_ts < ?",
                (day_start, day_end)
            ))
            effective_sent = max(sent, batch_sent)

            recv = len(self.store.rows(
                "SELECT msg_id FROM messages WHERE is_bot=0 AND ts >= ? AND ts < ?",
                (day_start, day_end)
            ))

            tok_row = self.store.rows(
                "SELECT SUM(tokens_total) as tok, AVG(latency_ms) as lat FROM usage_logs WHERE timestamp >= ? AND timestamp < ? AND event_type IN ('ai_chat', 'ai_batch')",
                (day_start, day_end)
            )
            tokens = (tok_row[0].get("tok") or 0) if tok_row else 0
            latency = round((tok_row[0].get("lat") or 0)) if tok_row else 0

            result.append({
                "date": day_str,
                "label": label,
                "sent": effective_sent,
                "received": recv,
                "tokens": tokens,
                "latency_ms": latency
            })

        return result

    def get_persona_breakdown(self):
        personas = self.store.rows("SELECT id, name, color FROM personas")
        if not personas:
            return []

        breakdown = []
        total_msgs = 0

        for p in personas:
            pid = p["id"]
            sent_count = len(self.store.rows(
                "SELECT id FROM usage_logs WHERE persona_id=? AND event_type='msg_sent'",
                (pid,)
            ))
            batch_count = len(self.store.rows(
                "SELECT id FROM daily_batch_items WHERE persona_id=? AND status='sent'",
                (pid,)
            ))
            effective_count = max(sent_count, batch_count)
            total_msgs += effective_count

            tok_row = self.store.rows(
                "SELECT SUM(tokens_total) as tok, AVG(latency_ms) as lat FROM usage_logs WHERE persona_id=?",
                (pid,)
            )
            tok = (tok_row[0].get("tok") or 0) if tok_row else 0
            lat = round((tok_row[0].get("lat") or 0)) if tok_row else 0

            mem_count = len(self.store.rows("SELECT id FROM persona_memories WHERE persona_id=?", (pid,)))

            breakdown.append({
                "persona_id": pid,
                "name": p["name"],
                "color": p.get("color") or "#2fc4b2",
                "messages_sent": effective_count,
                "tokens": tok,
                "avg_latency_ms": lat,
                "memories_count": mem_count,
                "share_pct": 0.0
            })

        # Calculate share
        for b in breakdown:
            b["share_pct"] = round((b["messages_sent"] / max(1, total_msgs)) * 100, 1)

        breakdown.sort(key=lambda x: x["messages_sent"], reverse=True)
        return breakdown

    def get_recent_activity(self, limit=25):
        rows = self.store.rows("""
            SELECT u.*, p.name as persona_name, p.color as persona_color, g.title as group_title
            FROM usage_logs u
            LEFT JOIN personas p ON p.id=u.persona_id
            LEFT JOIN groups g ON g.chat_id=u.chat_id
            ORDER BY u.timestamp DESC
            LIMIT ?
        """, (limit,))
        return rows
