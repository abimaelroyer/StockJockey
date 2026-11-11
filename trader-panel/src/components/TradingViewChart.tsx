import { useEffect, useRef } from "react";

interface Props {
    symbol: string;
}

const WIDGET_SRC = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";

// TradingView's embeddable Advanced Chart, on the daily candles the bot reads.
export default function TradingViewChart({ symbol }: Props) {
    const container = useRef<HTMLDivElement>(null);

    useEffect(() => {
        const el = container.current;
        if (!el) return;

        const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
        const widget = document.createElement("div");
        widget.className = "tradingview-widget-container__widget";
        widget.style.height = "100%";

        const script = document.createElement("script");
        script.src = WIDGET_SRC;
        script.type = "text/javascript";
        script.async = true;
        script.innerHTML = JSON.stringify({
            autosize: true,
            symbol,
            interval: "D",
            timezone: "America/New_York",
            theme: dark ? "dark" : "light",
            style: "1",
            locale: "en",
            allow_symbol_change: false,
            hide_side_toolbar: true,
            support_host: "https://www.tradingview.com",
        });

        // Insert on the next tick: if the effect is torn down first (React
        // StrictMode mounts twice in dev), the script never loads detached.
        const timer = setTimeout(() => el.replaceChildren(widget, script));
        return () => {
            clearTimeout(timer);
            el.replaceChildren();
        };
    }, [symbol]);

    // The embed script forces its container to height: 100%, so the fixed
    // height lives on the wrapper.
    return (
        <div className="chart">
            <div className="tradingview-widget-container" ref={container} />
        </div>
    );
}
