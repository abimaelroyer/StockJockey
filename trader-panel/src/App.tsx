import { useState, useEffect, useCallback } from "react";
import type { FormEvent } from "react";
import axios from "axios";
import {
    getBalance,
    getPositions,
    getTrades,
    getHealth,
    getPnL,
    getRobinhoodBalance,
    getConfig,
    getStatus,
    updateConfig,
} from "./services/trader";
import type { Position, Trade, Health, PnL, Config, Status, EditableConfig } from "./services/trader";
import TradingViewChart from "./components/TradingViewChart";
import "./App.css";

const PERIODS = [
    { key: "1d", label: "1D" },
    { key: "1w", label: "1W" },
    { key: "1m", label: "1M" },
    { key: "1y", label: "1Y" },
    { key: "ytd", label: "YTD" },
    { key: "all", label: "All" },
];

type NumberField = Exclude<keyof EditableConfig, "symbols">;

const NUMBER_FIELDS: { key: NumberField; label: string; min: number; step?: number }[] = [
    { key: "short_window", label: "Short MA (days)", min: 1 },
    { key: "long_window", label: "Long MA (days)", min: 2 },
    { key: "trade_dollars", label: "Dollars per trade", min: 1, step: 0.01 },
    { key: "lookback_days", label: "History (days)", min: 2 },
    { key: "check_interval_minutes", label: "Check every (min)", min: 1 },
];

function formatTimestamp(ts: string): { date: string; time: string } {
    const hasTimezone = ts.endsWith("Z") || /[+-]\d{2}:\d{2}$/.test(ts);
    const isoString = hasTimezone ? ts : ts + "Z";
    const d = new Date(isoString);
    const date = d.toLocaleDateString("en-US", {
        timeZone: "America/New_York",
        month: "2-digit",
        day: "2-digit",
    });
    const time = d.toLocaleTimeString("en-US", {
        timeZone: "America/New_York",
        hour: "numeric",
        minute: "2-digit",
        hour12: true,
    });
    return { date, time };
}

const money = (n: number | null | undefined) => (n == null ? "—" : `$${n.toFixed(2)}`);

function settled<T>(result: PromiseSettledResult<T>): T | undefined {
    return result.status === "fulfilled" ? result.value : undefined;
}

const shares = (q: number | string) => parseFloat(Number(q).toFixed(4)).toString();

type Draft = Omit<EditableConfig, "symbols"> & { symbols: string };

function SettingsForm({ config, onSaved }: { config: Config; onSaved: (c: Config) => void }) {
    const [draft, setDraft] = useState<Draft>({ ...config, symbols: config.symbols.join(", ") });
    const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
    const [saving, setSaving] = useState(false);

    const submit = async (e: FormEvent) => {
        e.preventDefault();
        setSaving(true);
        try {
            const symbols = draft.symbols.split(/[\s,]+/).filter(Boolean);
            const saved = await updateConfig({ ...draft, symbols });
            onSaved(saved);
            setMessage({ ok: true, text: "Saved. The bot picks this up on its next check." });
        } catch (err) {
            const text = axios.isAxiosError(err) && err.response?.data?.error;
            setMessage({ ok: false, text: text || "Could not save settings." });
        } finally {
            setSaving(false);
        }
    };

    return (
        <form className="settings" onSubmit={submit}>
            <label className="symbols-field">
                <span>Symbols (comma-separated)</span>
                <input
                    type="text"
                    value={draft.symbols}
                    placeholder="AAPL, MSFT, SPY"
                    onChange={(e) => setDraft({ ...draft, symbols: e.target.value.toUpperCase() })}
                />
            </label>
            <div className="settings-grid">
                {NUMBER_FIELDS.map((f) => (
                    <label key={f.key}>
                        <span>{f.label}</span>
                        <input
                            type="number"
                            min={f.min}
                            step={f.step ?? 1}
                            value={draft[f.key]}
                            onChange={(e) => setDraft({ ...draft, [f.key]: Number(e.target.value) })}
                        />
                    </label>
                ))}
            </div>
            <div className="settings-footer">
                <p className="hint">
                    Each buy spends the dollar amount on fractional shares. Paper / live mode is set in{" "}
                    <code>config.json</code> on the server, not here.
                </p>
                <button type="submit" disabled={saving}>
                    {saving ? "Saving…" : "Save settings"}
                </button>
            </div>
            {message && <p className={message.ok ? "save-ok" : "error"}>{message.text}</p>}
        </form>
    );
}

function App() {
    const [balance, setBalance] = useState<number | null>(null);
    const [rhBalance, setRhBalance] = useState<number | null>(null);
    const [positions, setPositions] = useState<Record<string, Position>>({});
    const [trades, setTrades] = useState<Trade[]>([]);
    const [health, setHealth] = useState<Health | null>(null);
    const [pnl, setPnl] = useState<PnL | null>(null);
    const [config, setConfig] = useState<Config | null>(null);
    const [status, setStatus] = useState<Status>({});
    const [period, setPeriod] = useState("all");
    const [selected, setSelected] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);

    const loadAll = useCallback(async (currentPeriod: string) => {
        const results = await Promise.allSettled([
            getBalance(),
            getRobinhoodBalance(),
            getPositions(),
            getTrades(),
            getHealth(),
            getPnL(currentPeriod),
            getConfig(),
            getStatus(),
        ]);
        const [b, rhB, p, t, h, pn, c, s] = results;
        if (b.status === "fulfilled") setBalance(b.value);
        setRhBalance(settled(rhB) ?? null);
        if (p.status === "fulfilled") setPositions(p.value);
        if (t.status === "fulfilled") setTrades(t.value);
        if (h.status === "fulfilled") setHealth(h.value);
        if (pn.status === "fulfilled") setPnl(pn.value);
        if (c.status === "fulfilled") setConfig(c.value);
        if (s.status === "fulfilled") setStatus(s.value);
        const failures = results.filter((r) => r.status === "rejected");
        const unauthorized = failures.some((r) => axios.isAxiosError(r.reason) && r.reason.response?.status === 401);
        if (failures.length === results.length) setError("Could not reach the trading server.");
        else if (unauthorized) setError("The server rejected the panel's API key — VITE_PANEL_KEY must match PANEL_API_KEY.");
        else setError(failures.length ? "Some panel data could not be loaded." : null);
    }, []);

    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- polling an external server
        loadAll(period);
        const interval = setInterval(() => loadAll(period), 30000);
        return () => clearInterval(interval);
    }, [period, loadAll]);

    const mode = config?.mode ?? "paper";
    const modePnl = pnl?.modes[mode];
    const shortLabel = `${config?.short_window ?? 20}-day MA`;
    const longLabel = `${config?.long_window ?? 50}-day MA`;
    const chartSymbol = config && selected && config.symbols.includes(selected) ? selected : config?.symbols[0];
    const rows = status.symbols ?? {};

    return (
        <div className="app-container">
            <header className="app-header">
                <h1>StockJockey</h1>
                <div className="badges">
                    <span className={`mode-badge mode-${mode}`}>{mode === "live" ? "LIVE · Robinhood" : "PAPER"}</span>
                    {health && (
                        <span className={`health-badge health-${health.status}`} title={health.detail ?? undefined}>
                            {health.status === "ok" ? "● Running" : `● ${health.status.replace("_", " ")}`}
                        </span>
                    )}
                </div>
            </header>

            {error && <p className="error">{error}</p>}

            {config && (
                <section className="strategy-card">
                    <div className="strategy-title">
                        <h2>
                            {config.short_window}/{config.long_window} SMA crossover
                        </h2>
                        <span>
                            {money(config.trade_dollars)} per buy · {config.symbols.length} symbol
                            {config.symbols.length === 1 ? "" : "s"}
                        </span>
                    </div>
                    <div className="table-wrap">
                        <table className="symbols-table">
                            <thead>
                                <tr>
                                    <th>Symbol</th>
                                    <th>Last close</th>
                                    <th>{shortLabel}</th>
                                    <th>{longLabel}</th>
                                    <th>Trend</th>
                                    <th>Last crossover</th>
                                </tr>
                            </thead>
                            <tbody>
                                {config.symbols.map((sym) => {
                                    const row = rows[sym];
                                    return (
                                        <tr key={sym} className={sym === chartSymbol ? "selected" : ""}>
                                            <td>
                                                <button
                                                    type="button"
                                                    className="symbol-button"
                                                    aria-pressed={sym === chartSymbol}
                                                    onClick={() => setSelected(sym)}
                                                >
                                                    {sym}
                                                </button>
                                            </td>
                                            {row?.error ? (
                                                <td colSpan={5} className="error">{row.error}</td>
                                            ) : row?.short_ma != null ? (
                                                <>
                                                    <td>{money(row.close)}</td>
                                                    <td>{money(row.short_ma)}</td>
                                                    <td>{money(row.long_ma)}</td>
                                                    <td className={row.trend === "above" ? "positive" : "negative"}>
                                                        {row.trend === "above" ? "Above ▲" : "Below ▼"}
                                                    </td>
                                                    <td>
                                                        {row.last_crossover
                                                            ? `${row.last_crossover.action} ${row.last_crossover.date} (${row.last_crossover.result})`
                                                            : "—"}
                                                    </td>
                                                </>
                                            ) : (
                                                <td colSpan={5} className="hint">Waiting for the next check during market hours.</td>
                                            )}
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>
                    <p className="hint">
                        Closes are the last completed trading day. Checked{" "}
                        {status.checked_at ? new Date(status.checked_at).toLocaleString() : "—"}. Tap a symbol to chart it.
                    </p>
                </section>
            )}

            {config && chartSymbol && (
                <section>
                    <TradingViewChart
                        symbol={chartSymbol}
                        shortWindow={config.short_window}
                        longWindow={config.long_window}
                    />
                </section>
            )}

            <section className="period-tabs">
                {PERIODS.map((p) => (
                    <button
                        key={p.key}
                        className={period === p.key ? "active" : ""}
                        onClick={() => setPeriod(p.key)}
                    >
                        {p.label}
                    </button>
                ))}
            </section>

            <section className="stats-grid">
                <div className="stat-card">
                    <span>Paper Balance</span>
                    <strong>{money(balance)}</strong>
                </div>
                <div className="stat-card">
                    <span>Robinhood Equity</span>
                    <strong>{rhBalance !== null ? money(rhBalance) : "Not connected"}</strong>
                </div>
                <div className="stat-card">
                    <span>Realized P&L ({mode})</span>
                    <strong className={modePnl && modePnl.pnl >= 0 ? "positive" : "negative"}>
                        {modePnl ? `${modePnl.pnl >= 0 ? "+" : "-"}$${Math.abs(modePnl.pnl).toFixed(2)}` : "—"}
                    </strong>
                </div>
                <div className="stat-card">
                    <span>Win Rate ({mode})</span>
                    <strong>
                        {modePnl ? `${modePnl.win_rate}% (${modePnl.wins}W / ${modePnl.losses}L)` : "—"}
                    </strong>
                </div>
            </section>

            {pnl && (
                <section className="trades-today">
                    <span>Trades today — 📝 Paper: {pnl.paper_today} · 💵 Live: {pnl.live_today}</span>
                </section>
            )}

            <section>
                <h2>Open Positions</h2>
                {Object.keys(positions).length === 0 ? (
                    <p className="empty">No open positions.</p>
                ) : (
                    <div className="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>Symbol</th>
                                    <th>Qty</th>
                                    <th>Entry</th>
                                    <th>Invested</th>
                                    <th>Mode</th>
                                </tr>
                            </thead>
                            <tbody>
                                {Object.entries(positions).map(([symbol, pos]) => (
                                    <tr key={symbol}>
                                        <td>{symbol}</td>
                                        <td>{shares(pos.quantity)}</td>
                                        <td>{money(pos.entry_price)}</td>
                                        <td>{money(pos.invested)}</td>
                                        <td>{pos.mode}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </section>

            <section>
                <h2>Recent Trades</h2>
                {trades.length === 0 ? (
                    <p className="empty">No trades yet. The bot trades only when a crossover happens.</p>
                ) : (
                    <div className="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>Date</th>
                                    <th>Time</th>
                                    <th>Symbol</th>
                                    <th>Action</th>
                                    <th>Qty</th>
                                    <th>Price</th>
                                    <th>Short MA</th>
                                    <th>Long MA</th>
                                    <th>Mode</th>
                                    <th>Source</th>
                                </tr>
                            </thead>
                            <tbody>
                                {[...trades]
                                    .reverse()
                                    .slice(0, 10)
                                    .map((t, i) => {
                                        const { date, time } = formatTimestamp(t.timestamp);
                                        return (
                                            <tr key={i} className={t.action === "BUY" ? "buy" : "sell"}>
                                                <td>{date}</td>
                                                <td>{time}</td>
                                                <td>{t.symbol}</td>
                                                <td>{t.action}</td>
                                                <td>{shares(t.quantity)}</td>
                                                <td>{money(parseFloat(t.price))}</td>
                                                <td>{t.short_ma ? parseFloat(t.short_ma).toFixed(2) : "—"}</td>
                                                <td>{t.long_ma ? parseFloat(t.long_ma).toFixed(2) : "—"}</td>
                                                <td>{t.mode}</td>
                                                <td>{t.source}</td>
                                            </tr>
                                        );
                                    })}
                            </tbody>
                        </table>
                    </div>
                )}
            </section>

            {config && (
                <section>
                    <h2>Strategy Settings</h2>
                    <SettingsForm
                        key={JSON.stringify(config)}
                        config={config}
                        onSaved={setConfig}
                    />
                </section>
            )}
        </div>
    );
}

export default App;
