import axios from "axios";

const api = axios.create({
    baseURL: import.meta.env.VITE_TRADER_URL,
    headers: { "X-API-Key": import.meta.env.VITE_PANEL_KEY },
});

export type Mode = "paper" | "live";

export interface Position {
    entry_price: number;
    quantity: number;
    invested: number;
    timestamp: string;
    mode: Mode;
}

export interface Trade {
    timestamp: string;
    symbol: string;
    action: "BUY" | "SELL";
    quantity: string;
    price: string;
    short_ma: string;
    long_ma: string;
    mode: Mode;
    source: "bot" | "tradingview";
}

export interface Health {
    status: "ok" | "stale" | "error" | "no_heartbeat";
    last_cycle?: string;
    minutes_since?: number;
    detail?: string | null;
}

export interface Config {
    symbol: string;
    short_window: number;
    long_window: number;
    trade_quantity: number;
    lookback_days: number;
    mode: Mode;
    check_interval_minutes: number;
    paper_starting_balance: number;
}

export type EditableConfig = Pick<
    Config,
    "symbol" | "short_window" | "long_window" | "trade_quantity" | "lookback_days" | "check_interval_minutes"
>;

export interface Status {
    symbol?: string;
    mode?: Mode;
    short_window?: number;
    long_window?: number;
    as_of?: string;
    close?: number;
    latest_price?: number;
    short_ma?: number;
    long_ma?: number;
    trend?: "above" | "below";
    signal?: "BUY" | "SELL" | null;
    last_crossover?: { date: string; action: "BUY" | "SELL"; result: string } | null;
    checked_at?: string;
}

export interface ModePnL {
    pnl: number;
    wins: number;
    losses: number;
    win_rate: number;
}

export interface PnL {
    period: string;
    trades_in_period: number;
    paper_today: number;
    live_today: number;
    modes: Record<Mode, ModePnL>;
}

export const getBalance = async (): Promise<number> => {
    const res = await api.get("/balance");
    return res.data.balance;
};

export const getPositions = async (): Promise<Record<string, Position>> => {
    const res = await api.get("/positions");
    return res.data;
};

export const getTrades = async (): Promise<Trade[]> => {
    const res = await api.get("/trades");
    return res.data;
};

export const getHealth = async (): Promise<Health> => {
    // /health skips auth on the server, but sending the header anyway is harmless
    const res = await api.get("/health");
    return res.data;
};

export const getPnL = async (period: string = "all"): Promise<PnL> => {
    const res = await api.get(`/pnl?period=${period}`);
    return res.data;
};

export const getRobinhoodBalance = async (): Promise<number | null> => {
    const res = await api.get("/robinhood-balance");
    return res.data.equity;
};

export const getConfig = async (): Promise<Config> => {
    const res = await api.get("/config");
    return res.data;
};

export const updateConfig = async (changes: Partial<EditableConfig>): Promise<Config> => {
    const res = await api.post("/config", changes);
    return res.data;
};

export const getStatus = async (): Promise<Status> => {
    const res = await api.get("/status");
    return res.data;
};
