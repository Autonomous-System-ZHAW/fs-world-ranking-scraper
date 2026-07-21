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
    total: DisciplineResult
    bp: DisciplineResult = field(default_factory=DisciplineResult)   # Business Plan
    cm: DisciplineResult = field(default_factory=DisciplineResult)   # Cost & Manufacturing
    ed: DisciplineResult = field(default_factory=DisciplineResult)   # Engineering Design
    sp: DisciplineResult = field(default_factory=DisciplineResult)   # Skidpad
    ds: DisciplineResult = field(default_factory=DisciplineResult)   # DV Skidpad
    ac: DisciplineResult = field(default_factory=DisciplineResult)   # Acceleration
    da: DisciplineResult = field(default_factory=DisciplineResult)   # DV Acceleration
    ax: DisciplineResult = field(default_factory=DisciplineResult)   # Autocross
    en: DisciplineResult = field(default_factory=DisciplineResult)   # Endurance
    ef: DisciplineResult = field(default_factory=DisciplineResult)   # Efficiency
    td: DisciplineResult = field(default_factory=DisciplineResult)   # Trackdrive/Endurance
    penalty: DisciplineResult = field(default_factory=DisciplineResult)


@dataclass
class EventInfo:
    competition_id: int
    event_id: int
    competition_name: str
    event_title: str
    event_class: str
    event_date: str = ""
    n_teams: int = 0
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


def parse_event_heading(soup) -> tuple[str, str, str, str]:
    """Extract team class, event date, country code and title from the event heading."""
    event_heading = None
    for container in soup.select("div.content div.textbox h1"):
        if container.find("span", class_=re.compile(r"team-class")):
            event_heading = container
            break

    if event_heading is None:
        raise ValueError("Event heading not found")
    
    team_class_span = event_heading.find("span", class_=re.compile(r"team-class"))
    team_class = team_class_span.get_text(" ", strip=True) if team_class_span else ""

    country = ""
    flag_span = event_heading.find("span", class_="flag")
    if flag_span is not None:
        flag_img = flag_span.find("img")
        if flag_img is not None and flag_img.get("src"):
            m = re.search(r"/country/([a-z]{2})\.svg", str(flag_img["src"]))
            if m:
                country = m.group(1).upper()

    for span in event_heading.find_all("span", class_=re.compile(r"team-class|flag")):
        span.decompose()

    heading_text = event_heading.get_text(" ", strip=True)
    parts = [part for part in re.split(r"\s+", heading_text) if part]
    if len(parts) >= 2:
        event_date = parts[0]
        event_title = " ".join(parts[1:])
    else:
        event_date = ""
        event_title = heading_text

    return team_class, event_date, country, event_title


def parse_event(sess: RateLimitedSession, comp_id: int, event_id: int) -> Optional[EventInfo]:
    url = f"{BASE_URL}/competition/{comp_id}/event/{event_id}"
    soup = sess.get(url)

    try:
        event_class, event_date, country_code, event_title = parse_event_heading(soup)
        print(event_class, event_date, country_code, event_title)
    except ValueError as e:
        print(f"Error parsing event heading: {e}")
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
        bp = _parse_discipline_cell(tds[3]) if len(tds) > 3 else DisciplineResult()
        cm = _parse_discipline_cell(tds[4]) if len(tds) > 4 else DisciplineResult()
        ed = _parse_discipline_cell(tds[3]) if len(tds) > 3 else DisciplineResult()
        sp = _parse_discipline_cell(tds[4]) if len(tds) > 4 else DisciplineResult()
        ds = _parse_discipline_cell(tds[5]) if len(tds) > 5 else DisciplineResult()
        ac = _parse_discipline_cell(tds[6]) if len(tds) > 6 else DisciplineResult()
        da = _parse_discipline_cell(tds[7]) if len(tds) > 7 else DisciplineResult()
        ax = _parse_discipline_cell(tds[6]) if len(tds) > 6 else DisciplineResult()
        en = _parse_discipline_cell(tds[7]) if len(tds) > 7 else DisciplineResult()
        ef = _parse_discipline_cell(tds[7]) if len(tds) > 7 else DisciplineResult()
        td_ = _parse_discipline_cell(tds[7]) if len(tds) > 7 else DisciplineResult()
        penalty = _parse_discipline_cell(tds[8]) if len(tds) > 8 else DisciplineResult()

        results.append(
            TeamResult(
                rank=rank,
                university=university,
                country=country,
                total=total,
                bp=bp,
                cm=cm,
                ed=ed,
                sp=sp,
                ds=ds,
                ac=ac,
                da=da,
                ax=ax,
                en=en,
                ef=ef,
                td=td_,
                penalty=penalty,
            )
        )

    return EventInfo(
        competition_id=comp_id,
        event_id=event_id,
        competition_name="",
        event_title=event_title,
        event_date=event_date,
        event_class=event_class,
        n_teams=len(results),
        results=results,
    )


def save_event_csv(event: EventInfo, out_dir: Path = OUTPUT_DIR):
    path = out_dir / f"{event.event_date}_{event.event_title}_{event.event_class}_{event.n_teams}-teams_{event.competition_id}_{event.event_id}.csv"
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
                "rank", "university", "country",
                "total_pts", "total_rank", "total_pct",
                "bp_pts", "bp_rank", "bp_pct",
                "cm_pts", "cm_rank", "cm_pct",
                "ed_pts", "ed_rank", "ed_pct",
                "sp_pts", "sp_rank", "sp_pct",
                "ds_pts", "ds_rank", "ds_pct",
                "ac_pts", "ac_rank", "ac_pct",
                "da_pts", "da_rank", "da_pct",
                "ax_pts", "ax_rank", "ax_pct",
                "en_pts", "en_rank", "en_pct",
                "ef_pts", "ef_rank", "ef_pct",
                "td_pts", "td_rank", "td_pct",
                "penalty_pts", "penalty_rank",
            ]
        )
        for r in event.results:
            writer.writerow(
                [
                    r.rank, r.university, r.country,
                    r.total.points, r.total.rank, r.total.percent,
                    r.bp.points, r.bp.rank, r.bp.percent,
                    r.cm.points, r.cm.rank, r.cm.percent,
                    r.ed.points, r.ed.rank, r.ed.percent,
                    r.sp.points, r.sp.rank, r.sp.percent,
                    r.ds.points, r.ds.rank, r.ds.percent,
                    r.ac.points, r.ac.rank, r.ac.percent,
                    r.da.points, r.da.rank, r.da.percent,
                    r.ax.points, r.ax.rank, r.ax.percent,
                    r.en.points, r.en.rank, r.en.percent,
                    r.ef.points, r.ef.rank, r.ef.percent,
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

    print("Competitions entdecken ...")
    comps = discover_competitions(sess)
    print(f"    -> {len(comps)} Competitions gefunden")

    all_events: list[EventInfo] = []

    for idx, comp in enumerate(comps):
        if comp["id"] != 8:
            continue
        print(f"Events fuer '{comp['name']}' (id={comp['id']}) suchen ...")
        try:
            events = discover_events_for_competition(sess, comp["id"])
        except requests.HTTPError as e:
            print(f"   ! Fehler: {e}")
            continue
        for ev in events:
            print(f"   -> Event {ev['id']}: {ev['label']}")
            try:
                info = parse_event(sess, comp["id"], ev["id"])
            except requests.HTTPError as e:
                print(f"   ! Event {ev['id']} Fehler: {e}")
                continue
            if info is None:
                continue  # kein DC-Event
            info.competition_name = comp["name"]
            all_events.append(info)
            path = save_event_csv(info)
            print(f"   -> Event gefunden: {info.event_title} ({info.n_teams} Teams) -> {path}")

    # print(f"\n=== Zusammenfassung: {len(all_events)} DC-Events gefunden ===\n")
    # for ev in all_events:
    #     funnel = attrition_funnel(ev)
    #     print(funnel)


if __name__ == "__main__":
    main()