#!/usr/bin/env python3
"""
FS-World.org Scraper — Driverless Cup (DC) Attrition-Analyse
================================================================

Es gibt KEINE offizielle API für fs-world.org (verifiziert: /api -> 404,
robots.txt -> 404/leer, keine AJAX/JSON-Endpoints im Frontend-Code).
Die Ranking-Tabellen sind serverseitig gerendertes HTML; DataTables.js
im Browser sortiert nur, laedt aber keine Daten nach.

Nutzung erfolgt unter der "FS-World Data License - Version 1.0"
(https://fs-world.org/license):
  - nicht kommerziell
  - Attribution: "FS-World.org", Lizenztext, Retrieval-Datum
  - kein konkurrierender Service
  - Aenderungen/Auswertungen als solche kennzeichnen

Aufbau des Scripts:
  1. discover_competitions()  -> alle Competition-IDs von /competition
  2. discover_dc_events(comp_id) -> alle Event-Links einer Competition,
     die auf DC (Driverless Cup) gefiltert werden (Klassenlabel "DC" im
     H1 der Event-Seite bzw. ueber die Event-Auswahl auf /ranking).
  3. parse_event(event_url) -> BeautifulSoup-Parse der Ergebnistabelle:
     Team, Land, Klasse (EV/CV/other), Total, ED, DS, DA, AX, TD, Penalty
     -- jeweils Punkte UND Rang (aus dem data-tooltip Text).
  4. Aggregation: Attrition-Funnel pro Event (angemeldet -> Skidpad/Acc
     -> Autocross -> Trackdrive) + DNF/DQ-Heuristik (Score == 0 in einer
     Pflichtdisziplin, nachdem ein gueltiger Run laut FS-Regeln nie 0
     Punkte ergeben wuerde -> 0 heisst i.d.R. "kein gueltiger Run").

WICHTIG: Sei ein guter Buerger. min_pause zwischen Requests einhalten
(Standard 1.5s), nicht parallelisieren, User-Agent mit Kontaktinfo setzen.
"""

import csv
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://fs-world.org"
HEADERS = {
    "User-Agent": "fsworld-dc-attrition-research/1.0 (non-commercial, contact: lucas@penrose.ch)"
}

OUTPUT_DIR = Path("fsworld_data")
OUTPUT_DIR.mkdir(exist_ok=True)


class RateLimitedSession:
    def __init__(self, min_pause: float = 1.5):
        self.min_pause = min_pause
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def get(self, url: str) -> BeautifulSoup:
        now = time.time()
        diff = now - self.last_request
        if diff < self.min_pause:
            time.sleep(self.min_pause - diff)
        resp = self.session.get(url, timeout=30)
        self.last_request = time.time()
        resp.raise_for_status()
        return BeautifulSoup(resp.content, "html.parser")


@dataclass
class DisciplineResult:
    points: Optional[float] = None
    rank: Optional[int] = None
    percent: Optional[float] = None


@dataclass
class TeamResult:
    rank: int
    university: str
    country: str
    car_class: str  # EV / CV / other
    total: DisciplineResult
    ed: DisciplineResult = field(default_factory=DisciplineResult)   # Engineering Design
    ds: DisciplineResult = field(default_factory=DisciplineResult)   # DV Skidpad
    da: DisciplineResult = field(default_factory=DisciplineResult)   # DV Acceleration
    ax: DisciplineResult = field(default_factory=DisciplineResult)   # Autocross
    td: DisciplineResult = field(default_factory=DisciplineResult)   # Trackdrive/Endurance
    penalty: DisciplineResult = field(default_factory=DisciplineResult)


@dataclass
class EventInfo:
    competition_id: int
    event_id: int
    competition_name: str
    event_title: str
    n_teams: int
    results: list = field(default_factory=list)


TOOLTIP_RE = re.compile(
    r"Points:\s*(-?[\d.]+)(?:.*?Percents:\s*(-?[\d.]+)%)?.*?Rank:\s*(\d+)",
    re.DOTALL,
)


def _parse_discipline_cell(td) -> DisciplineResult:
    span = td.find("span", attrs={"data-tooltip": True})
    if span is None:
        return DisciplineResult()
    tooltip = span["data-tooltip"]
    m = TOOLTIP_RE.search(tooltip)
    if not m:
        return DisciplineResult()
    points = float(m.group(1))
    percent = float(m.group(2)) if m.group(2) is not None else None
    rank = int(m.group(3))
    return DisciplineResult(points=points, rank=rank, percent=percent)


def discover_competitions(sess: RateLimitedSession) -> list[dict]:
    """Liste aller Competitions von /competition (Name + ID + Land)."""
    soup = sess.get(f"{BASE_URL}/competition")
    comps = []
    for a in soup.select("table.table tbody tr td a[href^='/competition/']"):
        href = a["href"]
        m = re.search(r"/competition/(\d+)$", href)
        if not m:
            continue
        comps.append({"id": int(m.group(1)), "name": a.get_text(strip=True)})
    return comps


def discover_events_for_competition(sess: RateLimitedSession, comp_id: int) -> list[dict]:
    """Findet Event-Links (competition/{id}/event/{eid}) auf der Competition-Seite."""
    soup = sess.get(f"{BASE_URL}/competition/{comp_id}")
    events = []
    for a in soup.select(f"a[href^='/competition/{comp_id}/event/']"):
        href = a["href"]
        m = re.search(r"/event/(\d+)$", href)
        if not m:
            continue
        events.append({"id": int(m.group(1)), "label": a.get_text(strip=True)})
    # de-dupe
    seen = set()
    uniq = []
    for e in events:
        if e["id"] not in seen:
            seen.add(e["id"])
            uniq.append(e)
    return uniq


def parse_event(sess: RateLimitedSession, comp_id: int, event_id: int) -> Optional[EventInfo]:
    url = f"{BASE_URL}/competition/{comp_id}/event/{event_id}"
    soup = sess.get(url)

    h1 = soup.find("h1")
    if h1 is None:
        return None
    class_span = h1.find("span", class_="team-class")
    event_class = class_span.get_text(strip=True) if class_span else "?"
    event_title = h1.get_text(" ", strip=True)

    # Nur Driverless Cup (DC) Events verarbeiten
    if event_class != "DC":
        return None

    table = soup.find("table", id="eventTable")
    if table is None:
        return None

    results = []
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        rank_txt = tds[0].get_text(strip=True)
        try:
            rank = int(rank_txt)
        except ValueError:
            continue

        uni_cell = tds[1]
        uni_link = uni_cell.find("a")
        university = uni_link.get_text(strip=True) if uni_link else uni_cell.get_text(strip=True)
        flag_img = uni_cell.find("img", class_="flag")
        country = ""
        if flag_img and flag_img.get("src"):
            m = re.search(r"/country/([a-z]{2})\.svg", flag_img["src"])
            if m:
                country = m.group(1).upper()
        class_span = uni_cell.find("span", class_="team-class")
        car_class = class_span.get_text(strip=True) if class_span else "?"

        # Spaltenreihenfolge gemaess Event-Tabelle:
        # [0]=rank [1]=uni [2]=total [3]=ed [4]=ds [5]=da [6]=ax [7]=td [8]=penalty ...
        total = _parse_discipline_cell(tds[2]) if len(tds) > 2 else DisciplineResult()
        ed = _parse_discipline_cell(tds[3]) if len(tds) > 3 else DisciplineResult()
        ds = _parse_discipline_cell(tds[4]) if len(tds) > 4 else DisciplineResult()
        da = _parse_discipline_cell(tds[5]) if len(tds) > 5 else DisciplineResult()
        ax = _parse_discipline_cell(tds[6]) if len(tds) > 6 else DisciplineResult()
        td_ = _parse_discipline_cell(tds[7]) if len(tds) > 7 else DisciplineResult()
        penalty = _parse_discipline_cell(tds[8]) if len(tds) > 8 else DisciplineResult()

        results.append(
            TeamResult(
                rank=rank,
                university=university,
                country=country,
                car_class=car_class,
                total=total,
                ed=ed,
                ds=ds,
                da=da,
                ax=ax,
                td=td_,
                penalty=penalty,
            )
        )

    return EventInfo(
        competition_id=comp_id,
        event_id=event_id,
        competition_name="",
        event_title=event_title,
        n_teams=len(results),
        results=results,
    )


def save_event_csv(event: EventInfo, out_dir: Path = OUTPUT_DIR):
    path = out_dir / f"dc_{event.competition_id}_{event.event_id}.csv"
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "# Source: FS-World.org | FS-World Data License - Version 1.0 | "
                f"Retrieved: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"
            ]
        )
        writer.writerow(
            [
                "rank", "university", "country", "car_class",
                "total_pts", "total_rank", "total_pct",
                "ed_pts", "ed_rank", "ed_pct",
                "ds_pts", "ds_rank", "ds_pct",
                "da_pts", "da_rank", "da_pct",
                "ax_pts", "ax_rank", "ax_pct",
                "td_pts", "td_rank", "td_pct",
                "penalty_pts", "penalty_rank",
            ]
        )
        for r in event.results:
            writer.writerow(
                [
                    r.rank, r.university, r.country, r.car_class,
                    r.total.points, r.total.rank, r.total.percent,
                    r.ed.points, r.ed.rank, r.ed.percent,
                    r.ds.points, r.ds.rank, r.ds.percent,
                    r.da.points, r.da.rank, r.da.percent,
                    r.ax.points, r.ax.rank, r.ax.percent,
                    r.td.points, r.td.rank, r.td.percent,
                    r.penalty.points, r.penalty.rank,
                ]
            )
    return path


def attrition_funnel(event: EventInfo) -> dict:
    """Zaehlt, wie viele Teams in jeder Stufe > 0 Punkte erzielt haben."""
    n = event.n_teams
    if n == 0:
        return {}
    skid_or_acc = sum(1 for r in event.results if (r.ds.points or 0) > 0 or (r.da.points or 0) > 0)
    ax = sum(1 for r in event.results if (r.ax.points or 0) > 0)
    td = sum(1 for r in event.results if (r.td.points or 0) > 0)
    ed_only = sum(
        1 for r in event.results
        if (r.ed.points or 0) > 0
        and (r.ds.points or 0) == 0
        and (r.da.points or 0) == 0
        and (r.ax.points or 0) == 0
        and (r.td.points or 0) == 0
    )
    penalized = sum(1 for r in event.results if (r.penalty.points or 0) < 0)
    return {
        "event": event.event_title,
        "n_teams": n,
        "skid_or_acc": skid_or_acc,
        "skid_or_acc_pct": round(100 * skid_or_acc / n),
        "autocross": ax,
        "autocross_pct": round(100 * ax / n),
        "trackdrive": td,
        "trackdrive_pct": round(100 * td / n),
        "ed_only_never_dynamic": ed_only,
        "ed_only_pct": round(100 * ed_only / n),
        "penalized_teams": penalized,
    }


def main():
    sess = RateLimitedSession(min_pause=1.5)

    print("1) Competitions entdecken ...")
    comps = discover_competitions(sess)
    print(f"   -> {len(comps)} Competitions gefunden")

    all_events: list[EventInfo] = []

    for comp in comps:
        print(f"2) Events fuer '{comp['name']}' (id={comp['id']}) suchen ...")
        try:
            events = discover_events_for_competition(sess, comp["id"])
        except requests.HTTPError as e:
            print(f"   ! Fehler: {e}")
            continue
        for ev in events:
            print(f"   -> Event {ev['id']}: {ev['label']}")
            # try:
            #     info = parse_event(sess, comp["id"], ev["id"])
            # except requests.HTTPError as e:
            #     print(f"   ! Event {ev['id']} Fehler: {e}")
            #     continue
            # if info is None:
            #     continue  # kein DC-Event
            # info.competition_name = comp["name"]
            # all_events.append(info)
            # path = save_event_csv(info)
            # print(f"   -> DC-Event gefunden: {info.event_title} ({info.n_teams} Teams) -> {path}")

    # print(f"\n=== Zusammenfassung: {len(all_events)} DC-Events gefunden ===\n")
    # for ev in all_events:
    #     funnel = attrition_funnel(ev)
    #     print(funnel)


if __name__ == "__main__":
    main()