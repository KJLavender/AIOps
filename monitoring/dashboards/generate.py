"""Generate the Grafana dashboard ConfigMaps in this folder.

    python monitoring/dashboards/generate.py && kubectl apply -f monitoring/dashboards/
"""
import json
import os

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
NS = "aiops-demo|travel"

# Categorical slots 1-4 of the reference palette (dark-surface steps; Grafana
# defaults to its dark theme). Status colors stay reserved for state tiles.
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500"]
GOOD, CRITICAL, WARNING = "#0ca30c", "#d03b3b", "#fab219"


def grid(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def stat(pid, title, expr, pos, unit="none", steps=None, desc="", decimals=0, legend="",
         instant=True, text_mode="value"):
    return {
        "id": pid, "type": "stat", "title": title, "description": desc, "datasource": PROM,
        "gridPos": pos,
        "targets": [{"refId": "A", "datasource": PROM, "expr": expr, "instant": instant,
                     "legendFormat": legend}],
        "fieldConfig": {"defaults": {
            "unit": unit, "decimals": decimals,
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": steps or [{"color": "text", "value": None}]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "none", "textMode": text_mode,
                    "justifyMode": "center", "orientation": "auto"},
    }


def timeseries(pid, title, targets, pos, unit="none", draw="line", overrides=None, desc="",
               points="auto", legend_calcs=None, min0=True, interval=None):
    return {
        "id": pid, "type": "timeseries", "title": title, "description": desc, "datasource": PROM,
        # Bars over increase(x[w]) need step == w, or the windows overlap into a solid wall.
        **({"interval": interval} if interval else {}),
        "gridPos": pos, "targets": targets,
        "fieldConfig": {"defaults": {
            "unit": unit, "min": 0 if min0 else None,
            "color": {"mode": "palette-classic"},
            "custom": {"drawStyle": draw, "lineWidth": 2, "fillOpacity": 0 if draw == "line" else 80,
                       "pointSize": 8, "showPoints": points, "spanNulls": True,
                       "barAlignment": 0, "axisSoftMin": 0 if min0 else None,
                       "gradientMode": "none", "stacking": {"mode": "none"}},
        }, "overrides": overrides or []},
        "options": {"legend": {"displayMode": "table" if legend_calcs else "list",
                               "placement": "bottom", "showLegend": True,
                               "calcs": legend_calcs or []},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def fixed_color(name, color):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}


def logs(pid, title, expr, pos):
    return {"id": pid, "type": "logs", "title": title, "datasource": LOKI, "gridPos": pos,
            "targets": [{"refId": "A", "datasource": LOKI, "expr": expr}],
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                        "enableLogDetails": True, "dedupStrategy": "none"}}


def row(pid, title, y):
    return {"id": pid, "type": "row", "title": title, "collapsed": False, "gridPos": grid(0, y, 24, 1),
            "panels": []}


def dashboard(uid, title, panels, tags, refresh="30s", time_from="now-24h"):
    return {"uid": uid, "title": title, "tags": tags, "timezone": "browser", "editable": True,
            "schemaVersion": 39, "refresh": refresh, "time": {"from": time_from, "to": "now"},
            "panels": panels, "templating": {"list": []}, "annotations": {"list": []}}


# --- AIOps Agent -----------------------------------------------------------
stages = ["detected", "remediated", "validated", "rolled_back"]
aiops = dashboard("aiops-agent", "AIOps Agent", [
    row(1, "Now", 0),
    stat(2, "Pods not ready", f'sum(kube_pod_status_ready{{condition="false",namespace=~"{NS}"}}) or vector(0)',
         grid(0, 1, 5, 4), steps=[{"color": GOOD, "value": None}, {"color": CRITICAL, "value": 1}],
         desc="Pods in the watched namespaces whose Ready condition is false"),
    stat(3, "Auto-fixes validated (24h)", 'sum(increase(aiops_events_total{stage="validated"}[24h])) or vector(0)',
         grid(5, 1, 5, 4), desc="Remediations that passed Running & Ready & stable"),
    stat(4, "Rollbacks (24h)", 'sum(increase(aiops_events_total{stage="rolled_back"}[24h])) or vector(0)',
         grid(10, 1, 5, 4), steps=[{"color": "text", "value": None}, {"color": WARNING, "value": 1}],
         desc="Fixes that failed validation and were undone"),
    stat(5, "Learned fixes (KB)", "aiops_kb_entries", grid(15, 1, 4, 4),
         desc="Verified fixes the agent has learned"),
    stat(6, "Last scan", "time() - aiops_last_scan_timestamp_seconds", grid(19, 1, 5, 4), unit="s",
         steps=[{"color": GOOD, "value": None}, {"color": CRITICAL, "value": 120}],
         desc="Seconds since the agent last scanned the cluster (poll interval 20s)"),
    row(7, "Activity", 5),
    timeseries(8, "Agent pipeline events (per 10 min)",
               [{"refId": s[0].upper() + str(i), "datasource": PROM, "legendFormat": s,
                 "expr": f'sum(increase(aiops_events_total{{stage="{s}"}}[10m])) or vector(0)'}
                for i, s in enumerate(stages)],
               grid(0, 6, 14, 8), draw="bars", interval="10m",
               overrides=[fixed_color(s, SERIES[i]) for i, s in enumerate(stages)],
               desc="detected -> remediated -> validated; rolled_back = fix undone after failing validation"),
    {
        "id": 9, "type": "bargauge", "title": "Container restarts (last 1h)", "datasource": PROM,
        "gridPos": grid(14, 6, 10, 8),
        "targets": [{"refId": "A", "datasource": PROM, "instant": True, "legendFormat": "{{namespace}}/{{pod}}",
                     "expr": f'topk(8, sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total{{namespace=~"{NS}"}}[1h])) > 0)'}],
        "fieldConfig": {"defaults": {"unit": "none", "decimals": 0, "min": 0,
                                     "color": {"mode": "fixed", "fixedColor": SERIES[0]}}, "overrides": []},
        "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                    "valueMode": "text", "namePlacement": "left", "sizing": "manual",
                    "minVizHeight": 24, "maxVizHeight": 32, "text": {"titleSize": 13, "valueSize": 16},
                    "reduceOptions": {"calcs": ["lastNotNull"], "values": False}},
    },
    row(10, "Logs (Loki)", 14),
    logs(11, "Agent log", '{namespace="aiops-demo", container="agent"}', grid(0, 15, 14, 12)),
    logs(12, "Kubernetes warning events", f'{{job="kubernetes-events", namespace=~"{NS}"}} |= "type=Warning"',
         grid(14, 15, 10, 12)),
    # Evaluate / Protect / Monitor / Simulate (after future-agi's feedback loop).
    row(13, "LLM decision quality", 27),
    stat(14, "Judge approved (7d)", 'sum(increase(aiops_events_total{stage="judge_passed"}[7d])) or vector(0)',
         grid(0, 28, 4, 4), desc="LLM-proposed fixes the second LLM (judge) approved"),
    stat(15, "Judge rejected (7d)", 'sum(increase(aiops_events_total{stage="judge_rejected"}[7d])) or vector(0)',
         grid(4, 28, 4, 4), steps=[{"color": "text", "value": None}, {"color": WARNING, "value": 1}],
         desc="Proposals not grounded in the cluster evidence, or not fitting the root cause"),
    stat(16, "Poisoned results blocked (7d)", 'sum(increase(aiops_events_total{stage="guard_blocked"}[7d])) or vector(0)',
         grid(8, 28, 4, 4), steps=[{"color": "text", "value": None}, {"color": WARNING, "value": 1}],
         desc="Search results dropped by the prompt-injection guard before reaching the LLM"),
    logs(17, "Nightly simulation (aiops-eval)", '{namespace="aiops-demo", container="eval"} |= "sim_summary"',
         grid(12, 28, 12, 4)),
    logs(18, "LLM decision records", '{namespace="aiops-demo", container="agent"} |= "decision "',
         grid(0, 32, 24, 10)),
], ["aiops", "kubernetes"])

# --- Flight Deals ----------------------------------------------------------
flights = dashboard("flight-deals", "Flight Deals", [
    row(1, "Current fares", 0),
    stat(2, "Cheapest fare per route (TWD)", "max by (watch_id, route) (flightwatch_price)", grid(0, 1, 16, 5), unit="none",
         legend="#{{watch_id}} {{route}}", text_mode="value_and_name",
         desc="Latest cheapest fare found in each route's date window"),
    stat(3, "Routes watched", "flightwatch_watches_active", grid(16, 1, 4, 5)),
    stat(4, "Stalest route check", "time() - min(flightwatch_last_check_timestamp_seconds)", grid(20, 1, 4, 5),
         unit="s", steps=[{"color": GOOD, "value": None}, {"color": CRITICAL, "value": 4 * 3600}],
         desc="Age of the least recently checked route. Checks run every 3h; red after 4h = scheduler stuck"),
    row(5, "History", 6),
    timeseries(6, "Cheapest fare over time (TWD)",
               [{"refId": "A", "datasource": PROM, "expr": "max by (watch_id, route) (flightwatch_price)",
                 "legendFormat": "#{{watch_id}} {{route}}"}],
               grid(0, 7, 16, 10), unit="none", points="always", legend_calcs=["lastNotNull", "min"],
               min0=False, desc="One line per watched route; points = checks (every 3h)"),
    {
        "id": 7, "type": "table", "title": "Routes", "datasource": PROM, "gridPos": grid(16, 7, 8, 10),
        "targets": [
            {"refId": "A", "datasource": PROM, "expr": "sum by (watch_id, route) (flightwatch_price)", "instant": True, "format": "table"},
            {"refId": "B", "datasource": PROM, "expr": "sum by (watch_id, route) (flightwatch_lowest_price)", "instant": True, "format": "table"},
            {"refId": "C", "datasource": PROM, "expr": "sum by (watch_id, route) (flightwatch_target_price)", "instant": True, "format": "table"},
        ],
        "transformations": [
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True, "__name__": True, "container": True, "endpoint": True,
                                  "instance": True, "job": True, "namespace": True, "pod": True,
                                  "service": True, "currency": True, "codes": True},
                "renameByName": {"watch_id": "#", "route": "Route", "Value #A": "Now",
                                 "Value #B": "Lowest", "Value #C": "Target"},
                "indexByName": {"watch_id": 0, "route": 1, "Value #A": 2, "Value #B": 3, "Value #C": 4}}},
        ],
        "fieldConfig": {"defaults": {"decimals": 0, "custom": {"align": "auto"}}, "overrides": []},
        "options": {"showHeader": True, "sortBy": [{"displayName": "#", "desc": False}]},
    },
    row(8, "Service", 17),
    timeseries(9, "Price checks (per 3h)",
               [{"refId": "A", "datasource": PROM, "legendFormat": "{{result}}",
                 "expr": "sum by (result) (increase(flightwatch_checks_total[3h]))"}],
               grid(0, 18, 8, 8), draw="bars", interval="3h",
               overrides=[fixed_color("ok", SERIES[0]), fixed_color("empty", SERIES[3]),
                          fixed_color("error", SERIES[1])]),
    timeseries(10, "Chat commands (per 1h)",
               [{"refId": "A", "datasource": PROM, "legendFormat": "{{action}}",
                 "expr": "sum by (action) (increase(flightwatch_commands_total[1h]))"}],
               grid(8, 18, 8, 8), draw="bars", interval="1h"),
    logs(11, "flight-watcher log", '{namespace="travel", container="app"}', grid(16, 18, 8, 8)),
], ["travel", "flights"], refresh="1m", time_from="now-7d")


def configmap(name, folder, dash):
    body = json.dumps(dash, ensure_ascii=False, indent=1)
    indented = "\n".join("    " + line for line in body.splitlines())
    return f"""# Generated dashboard; Grafana's sidecar loads every ConfigMap labelled grafana_dashboard.
apiVersion: v1
kind: ConfigMap
metadata:
  name: {name}
  namespace: monitoring
  labels:
    grafana_dashboard: "1"
  annotations:
    grafana_folder: {folder}
data:
  {name}.json: |-
{indented}
"""


# --- Logs: browse Loki without Explore (anonymous viewers can't use Explore) ---
def loki_var(name, label, query):
    return {"name": name, "label": label, "type": "query", "datasource": LOKI, "query": query,
            "refresh": 2, "includeAll": name == "app", "allValue": ".+", "multi": False, "sort": 1,
            "current": {}}


LOG_SELECTOR = '{namespace="$namespace", app=~"$app"} |~ "(?i)$search"'
logs_dash = dashboard("logs", "Logs", [
    {"id": 1, "type": "timeseries", "title": "Log lines per service", "datasource": LOKI,
     "gridPos": grid(0, 0, 24, 6), "interval": "1m",
     "targets": [{"refId": "A", "datasource": LOKI, "legendFormat": "{{app}}",
                  "expr": f"sum by (app) (count_over_time({LOG_SELECTOR} [$__auto]))"}],
     "fieldConfig": {"defaults": {"min": 0, "decimals": 0, "color": {"mode": "palette-classic"},
                                  "custom": {"drawStyle": "bars", "fillOpacity": 80, "stacking": {"mode": "normal"}}},
                     "overrides": []},
     "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                 "tooltip": {"mode": "multi", "sort": "desc"}}},
    logs(2, "Logs", LOG_SELECTOR, grid(0, 6, 24, 22)),
], ["logs", "loki"], refresh="30s", time_from="now-1h")
logs_dash["templating"]["list"] = [
    loki_var("namespace", "Namespace", "label_values(namespace)"),
    loki_var("app", "Service", 'label_values({namespace="$namespace"}, app)'),
    {"name": "search", "label": "Search", "type": "textbox", "query": "", "current": {"value": ""}},
]

out = os.path.dirname(os.path.abspath(__file__))
for name, folder, dash in (("aiops-agent", "AIOps", aiops), ("flight-deals", "Travel", flights),
                           ("logs", "Logs", logs_dash)):
    with open(os.path.join(out, name + ".yaml"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(configmap("dashboard-" + name, folder, dash))
print("written")
