"""Luck model: compare every random event in a replay with its odds.

For each event with odds p (0 < p < 1) and outcome x (1 if it happened, else 0), the side the
outcome favours gains (x - p) "lucky events" and the other side loses the same amount. A player's
score is their net divided by sqrt(sum p(1-p)), i.e. a z-score, and the percentile is the normal CDF.

Modelled: critical hits, accuracy, secondary burn/freeze/paralysis/flinch/stat-drop/other effects,
full paralysis and thawing. Not modelled (not visible in a replay): hidden items and abilities such
as Scope Lens, Serene Grace or Compound Eyes; speed ties; confusion; sleep length; damage rolls.
"""
import json
import math
import os
from collections import defaultdict, OrderedDict

REF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ref")
with open(os.path.join(REF, "moves.json")) as _f:
    MOVES = json.load(_f)
with open(os.path.join(REF, "species.json")) as _f:
    SPECIES = json.load(_f)

CRIT_P = [1 / 16, 1 / 8, 1 / 4, 1 / 3, 1 / 2]  # Gen 5 crit stages
NO_CRIT = {"Super Fang", "Endeavor", "Counter", "Mirror Coat", "Pain Split", "Final Gambit", "Bide",
           "Metal Burst", "Psywave", "Struggle", "Beat Up"}
STATUS_IMMUNE = {"brn": {"Fire"}, "frz": {"Ice"}, "psn": {"Poison", "Steel"}, "tox": {"Poison", "Steel"}}
STATUSES = ("brn", "frz", "par", "psn", "tox", "slp")
VERB = {"brn": "burned", "frz": "froze", "par": "paralyzed", "psn": "poisoned", "tox": "poisoned",
        "slp": "put to sleep", "flinch": "flinched", "drop": "lowered the stats of", "other": "affected",
        "confusion": "confused"}

# kind -> (section label, row label for the beneficiary, row label for the victim)
KINDS = OrderedDict([
    ("crit", ("Critical hits", "Crits you landed", "Crits you took")),
    ("acc", ("Accuracy", "Your moves hitting", "Opponent moves hitting you")),
    ("brn", ("Burns", "Burns you inflicted", "Burns you took")),
    ("frz", ("Freezes", "Freezes you inflicted", "Freezes you took")),
    ("par", ("Paralysis", "Paralysis you inflicted", "Paralysis you took")),
    ("flinch", ("Flinches", "Flinches you caused", "Flinches you took")),
    ("drop", ("Stat drops", "Stat drops you caused", "Stat drops you took")),
    ("conf", ("Confusion", "Confusion you caused", "Confusion you took")),
    ("other", ("Other effects", "Other effects you caused", "Other effects you took")),
    ("fpar", ("Full paralysis", "Opponent fully paralyzed", "You fully paralyzed")),
    ("cself", ("Confusion self-hits", "Opponent hit itself in confusion", "You hit yourself in confusion")),
    ("thaw", ("Thawing", "Your Pokémon thawing out", "Opponent's Pokémon thawing out")),
])
NET_COLUMN = {"crit": "crit_net", "acc": "acc_net", "brn": "brn_net", "frz": "frz_net", "par": "par_net",
              "flinch": "flinch_net", "drop": "other_net", "other": "other_net", "conf": "other_net",
              "fpar": "deny_net", "cself": "deny_net", "thaw": "deny_net"}



def opp(side):
    return "p2" if side == "p1" else "p1"


def acc_multiplier(stage):
    stage = max(-6, min(6, stage))
    return (3 + stage) / 3 if stage >= 0 else 3 / (3 - stage)


class _Parser:
    def __init__(self, log):
        self.log = log.split("\n")
        self.nick = {}  # "p1a: Mew" -> (side, species)
        self.status = {}  # (side, species) -> major status
        self.boosts = defaultdict(lambda: defaultdict(int))  # pos -> stat -> stage
        self.focus = set()
        self.confused = set()
        self.weather = None
        self.turn = 0
        self.moved = set()  # slots ("p1a") that have acted this turn
        self.pending_flinch = {}  # target slot -> event waiting on the target's action
        self.events = []
        self.i = 0

    # -- small helpers ---------------------------------------------------------------------
    def species(self, pos):
        w = self.nick.get(pos)
        return w[1] if w else "?"

    def add(self, kind, ben, p, x, text, **extra):
        if p <= 0 or p >= 1:
            return None
        ev = {"turn": self.turn, "kind": kind, "ben": ben, "p": p, "x": 1 if x else 0, "text": text}
        ev.update(extra)
        self.events.append(ev)
        return ev

    def lookup(self, pos):
        """Find a mon by position. Party-wide cures name it as 'p1: Suicune' with no slot letter."""
        w = self.nick.get(pos)
        if w or ": " not in pos:
            return w
        side, name = pos.split(": ", 1)
        for key, val in self.nick.items():
            if key[:2] == side and key.split(": ", 1)[1] == name:
                return val
        return None

    def set_status(self, pos, st):
        w = self.nick.get(pos)
        if w:
            self.status[w] = st

    def cure(self, pos, st, tags):
        w = self.lookup(pos)
        if w and self.status.get(w) == st:
            if st == "frz" and not any(x.startswith("[from]") for x in tags):
                self.add("thaw", w[0], 0.2, 1, "%s thawed out" % w[1])
            self.status.pop(w, None)

    # -- main loop -------------------------------------------------------------------------
    def run(self):
        n = len(self.log)
        while self.i < n:
            line = self.log[self.i]
            self.i += 1
            if not line.startswith("|"):
                continue
            p = line.split("|")
            t = p[1]
            if t in ("switch", "drag"):
                pos = p[2]
                side = pos[:2]
                sp = p[3].split(",")[0].strip()
                self.nick[pos] = (side, sp)
                self.boosts[pos].clear()
                self.focus.discard(pos)
                self.confused.discard(pos)
                st = p[4].split(" ")[-1] if len(p) > 4 else ""
                if st in STATUSES:
                    self.status[(side, sp)] = st
                self.moved.discard(pos[:3])
            elif t == "turn":
                self.turn = int(p[2])
                self.moved.clear()
                self.pending_flinch.clear()
            elif t == "upkeep":
                self.pending_flinch.clear()
            elif t == "-weather":
                if not (len(p) > 3 and p[3] == "[upkeep]"):
                    self.weather = None if p[2] == "none" else p[2]
            elif t == "-status":
                self.set_status(p[2], p[3])
            elif t == "-curestatus":
                self.cure(p[2], p[3], p[4:])
            elif t == "-start" and len(p) > 3 and p[3] == "move: Focus Energy":
                self.focus.add(p[2])
            elif t == "-start" and len(p) > 3 and p[3] == "confusion":
                self.confused.add(p[2])
            elif t == "-end" and len(p) > 3 and p[3] == "confusion":
                self.confused.discard(p[2])
            elif t == "-activate" and len(p) > 3 and p[3] == "confusion":
                self.add("cself", opp(p[2][:2]), 0.5, 1, "%s hit itself in confusion" % self.species(p[2]))
            elif t in ("-boost", "-unboost") and len(p) > 4 and p[3] in ("accuracy", "evasion"):
                self.boosts[p[2]][p[3]] += int(p[4]) * (1 if t == "-boost" else -1)
            elif t in ("-clearallboost",):
                self.boosts.clear()
            elif t == "cant":
                self.on_cant(p)
            elif t == "move":
                self.on_move(p)
        return self.events

    def on_cant(self, p):
        pos = p[2]
        reason = p[3] if len(p) > 3 else ""
        self.moved.add(pos[:3])
        side = pos[:2]
        if reason == "par":
            self.add("fpar", opp(side), 0.25, 1, "%s was fully paralyzed" % self.species(pos))
        elif reason == "frz":
            self.add("thaw", side, 0.2, 0, "%s stayed frozen" % self.species(pos))
        elif reason == "flinch":
            pf = self.pending_flinch.pop(pos[:3], None)
            if pf:
                pf["x"] = 1
                self.events.append(pf)

    def on_move(self, p):
        pos = p[2]
        if pos not in self.nick:
            return
        side = pos[:2]
        name = p[3]
        extra = p[5:] if len(p) > 5 else []
        indirect = any(x.startswith("[from]") for x in extra)
        pf = self.pending_flinch.pop(pos[:3], None)  # the target got to act: it didn't flinch
        if pf:
            self.events.append(pf)
        self.moved.add(pos[:3])
        if not indirect and pos in self.confused:
            self.add("cself", opp(side), 0.5, 0, "%s attacked through confusion" % self.species(pos))
        if not indirect and self.status.get(self.nick[pos]) == "par":
            self.add("fpar", opp(side), 0.25, 0, "%s moved despite paralysis" % self.species(pos))
        seg = []
        while self.i < len(self.log):
            nxt = self.log[self.i]
            if nxt.startswith(("|move|", "|switch|", "|drag|", "|upkeep", "|turn|", "|cant|", "|win|")):
                break
            seg.append(nxt.split("|"))
            self.i += 1
        if not indirect:
            self.resolve_move(side, pos, name, seg)
        self.apply_segment_state(pos, seg)

    def apply_segment_state(self, pos, seg):
        for q in seg:
            if len(q) < 3:
                continue
            if q[1] == "-status":
                self.set_status(q[2], q[3])
            elif q[1] == "-curestatus":
                self.cure(q[2], q[3], q[4:])
            elif q[1] in ("-boost", "-unboost") and len(q) > 4 and q[3] in ("accuracy", "evasion"):
                self.boosts[q[2]][q[3]] += int(q[4]) * (1 if q[1] == "-boost" else -1)
            elif q[1] == "-start" and len(q) > 3 and q[3] == "move: Focus Energy":
                self.focus.add(q[2])
            elif q[1] == "-start" and len(q) > 3 and q[3] == "confusion":
                self.confused.add(q[2])
            elif q[1] == "-end" and len(q) > 3 and q[3] == "confusion":
                self.confused.discard(q[2])
            elif q[1] == "-activate" and len(q) > 3 and q[3] == "confusion":
                self.add("cself", opp(q[2][:2]), 0.5, 1, "%s hit itself in confusion" % self.species(q[2]))

    # -- one move --------------------------------------------------------------------------
    def resolve_move(self, side, pos, name, seg):
        mv = MOVES.get(name)
        if not mv:
            return
        res = defaultdict(lambda: {"miss": False, "hits": [], "sub": False, "blocked": False,
                                   "immune": False, "status": set(), "drop": False, "confusion": False,
                                   "fainted": False})
        crit_pending = set()
        for q in seg:
            if len(q) < 3:
                continue
            k = q[1]
            tag_from = any(x.startswith("[from]") for x in q[3:])
            if k == "-miss" and len(q) > 3:
                res[q[3]]["miss"] = True
            elif k == "-crit":
                crit_pending.add(q[2])
            elif k == "-damage" and not tag_from:
                r = res[q[2]]
                r["hits"].append(q[2] in crit_pending)
                crit_pending.discard(q[2])
                if q[3].endswith("fnt"):
                    r["fainted"] = True
            elif k == "-activate" and len(q) > 3:
                if q[3] in ("move: Protect", "move: Detect", "move: Wide Guard", "move: Quick Guard",
                            "move: Endure"):
                    res[q[2]]["blocked"] = True
                elif q[3] == "Substitute" or q[3].startswith("Substitute"):
                    res[q[2]]["sub"] = True
            elif k in ("-immune", "-fail", "-notarget", "-block"):
                if len(q) > 2:
                    res[q[2]]["immune"] = True
            elif k == "-status" and not tag_from:
                res[q[2]]["status"].add(q[3])
            elif k == "-unboost" and not tag_from:
                res[q[2]]["drop"] = True
            elif k == "-start" and len(q) > 3 and q[3] == "confusion" and not tag_from:
                res[q[2]]["confusion"] = True
        atk = self.species(pos)
        for tpos, r in list(res.items()):
            if tpos[:2] == side or tpos not in self.nick:
                continue  # allies and the user aren't part of this model
            tsp = self.species(tpos)
            responded = bool(r["hits"] or r["status"] or r["drop"] or r["confusion"] or r["sub"])
            # ---- accuracy ----------------------------------------------------------------
            acc = mv["acc"]
            if (acc is not True and not mv["ohko"] and not r["blocked"] and not r["immune"]
                    and (r["miss"] or responded)):
                shown = acc
                if name in ("Thunder", "Hurricane"):
                    if self.weather == "RainDance":
                        acc = None
                    elif self.weather == "SunnyDay":
                        acc = 50
                elif name == "Blizzard" and self.weather == "Hail":
                    acc = None
                if acc:
                    stage = self.boosts[pos]["accuracy"] - self.boosts[tpos]["evasion"]
                    prob = min(1.0, acc / 100 * acc_multiplier(stage))
                    pct = round(prob * 100)
                    self.add("acc", side, prob, not r["miss"],
                             "%s's %s %s %s (%d%% accurate)" % (atk, name, "missed" if r["miss"] else "hit", tsp, pct),
                             actor=side)
            if r["miss"] or r["blocked"] or r["immune"]:
                continue
            # ---- critical hits -----------------------------------------------------------
            damaging = mv["cat"] != "Status" and not mv["fixed"] and not mv["ohko"] and name not in NO_CRIT
            if damaging and not mv["willCrit"]:
                stage = (mv["crit"] - 1) + (2 if pos in self.focus else 0)
                prob = CRIT_P[min(stage, 4)]
                for was_crit in r["hits"]:
                    self.add("crit", side, prob, was_crit,
                             "%s's %s %s on %s" % (atk, name, "was a critical hit" if was_crit else "wasn't a crit", tsp))
            # ---- secondary effects -------------------------------------------------------
            if not r["hits"] or r["sub"] or r["fainted"]:
                continue
            prior = self.status.get(self.nick[tpos])
            types = SPECIES.get(tsp, [])
            for sec in mv["sec"]:
                kind, chance = sec["kind"], sec["chance"]
                prob = chance / 100
                if prob >= 1:
                    continue
                if kind in STATUSES:
                    if prior or any(t in STATUS_IMMUNE.get(kind, ()) for t in types):
                        continue
                    happened = any(s in r["status"] for s in ((kind, "tox") if kind == "psn" else (kind,)))
                    key = "psn" if kind in ("psn", "tox") else kind
                    self.add(key if key in KINDS else "other", side, prob, happened,
                             "%s's %s %s %s (%d%%)" % (atk, name, VERB[kind], tsp, chance))
                elif kind == "confusion":
                    self.add("conf", side, prob, r["confusion"],
                             "%s's %s %s %s (%d%%)" % (atk, name, VERB[kind], tsp, chance))
                elif kind == "drop":
                    self.add("drop", side, prob, r["drop"],
                             "%s's %s %s %s (%d%%)" % (atk, name, VERB["drop"], tsp, chance))
                elif kind == "flinch":
                    if tpos[:3] in self.moved:
                        continue
                    ev = {"turn": self.turn, "kind": "flinch", "ben": side, "p": prob, "x": 0,
                          "text": "%s's %s flinched %s (%d%%)" % (atk, name, tsp, chance)}
                    self.pending_flinch[tpos[:3]] = ev
                else:
                    happened = bool(r["status"] or r["confusion"])
                    self.add("other", side, prob, happened,
                             "%s's %s affected %s (%d%%)" % (atk, name, tsp, chance))


def luck_for_game(g):
    """Return the list of random events in one parsed game."""
    events = _Parser(g["rep"]["log"]).run()
    for e in events:
        if e["kind"] == "psn":  # poison isn't tracked separately; fold into "other"
            e["kind"] = "other"
    return events


def _phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def aggregate_luck(games, out_games):
    """Build luck, luckdetail, luckgames and luckmoments rows from every game's events."""
    detail = defaultdict(lambda: {"tries": 0, "happened": 0, "expected": 0.0, "luck": 0.0, "var": 0.0})
    players = defaultdict(lambda: {"net": 0.0, "var": 0.0, "events": 0, "nets": defaultdict(float),
                                   "cf": 0, "cfe": 0.0, "ca": 0, "cae": 0.0})
    game_rows, moments = [], []
    for g, og in zip(games, out_games):
        names = g["names"]
        net1 = 0.0
        var = 0.0
        crits = {"p1": 0, "p2": 0}
        for e in g["luck"]:
            ben = e["ben"]
            vic = opp(ben)
            d = e["x"] - e["p"]
            v = e["p"] * (1 - e["p"])
            var += v
            net1 += d if ben == "p1" else -d
            if e["kind"] == "crit" and e["x"]:
                crits[ben] += 1
            for side, role, lk in ((ben, "ben", d), (vic, "vic", -d)):
                player = names[side]
                row = detail[(player, e["kind"], role)]
                row["tries"] += 1
                row["happened"] += e["x"]
                row["expected"] += e["p"]
                row["luck"] += lk
                row["var"] += v
                pl = players[player]
                pl["net"] += lk
                pl["var"] += v
                pl["events"] += 1
                pl["nets"][NET_COLUMN[e["kind"]]] += lk
                if e["kind"] == "crit":
                    if role == "ben":
                        pl["cf"] += e["x"]
                        pl["cfe"] += e["p"]
                    else:
                        pl["ca"] += e["x"]
                        pl["cae"] += e["p"]
            odds = e["p"] if e["x"] else 1 - e["p"]
            if e["kind"] != "crit" and odds <= 0.3:
                lucky_side = ben if d > 0 else vic
                moments.append({"odds": odds, "order": (og["week"], og["id"], e["turn"]),
                                "row": OrderedDict([
                                    ("game", og["id"]), ("week", og["week"]), ("gnum", og["game"]),
                                    ("matchup", og["matchup"]), ("turn", e["turn"]), ("what", e["text"]),
                                    ("odds", round(odds, 2)), ("lucky", names[lucky_side]),
                                    ("unlucky", names[opp(lucky_side)])])})
        sigma = math.sqrt(var) if var else 0.0
        luckier = "p1" if net1 > 0 else ("p2" if net1 < 0 else None)
        game_rows.append(OrderedDict([
            ("id", og["id"]), ("replay", og["replay"]), ("week", og["week"]), ("game", og["game"]),
            ("matchup", og["matchup"]), ("player1", og["player1"]), ("player2", og["player2"]),
            ("winner", og["winner"]), ("turns", og["turns"]),
            ("luckier", names[luckier] if luckier else "Even"),
            ("edge", round(abs(net1), 2)), ("edge_score", round(abs(net1) / sigma, 2) if sigma else 0.0),
            ("luckier_won", ("Yes" if names[luckier] == og["winner"] else "No") if luckier else "n/a"),
            ("crits1", crits["p1"]), ("crits2", crits["p2"]), ("events", len(g["luck"])),
        ]))

    detail_rows = []
    for player in sorted({k[0] for k in detail}):
        for kind, (label, ben_label, vic_label) in KINDS.items():
            for role, text in (("ben", ben_label), ("vic", vic_label)):
                row = detail.get((player, kind, role))
                if not row:
                    continue
                sigma = math.sqrt(row["var"])
                detail_rows.append(OrderedDict([
                    ("id", "%s|%s|%s" % (player, kind, role)), ("player", player),
                    ("event", text), ("kind", label), ("tries", row["tries"]),
                    ("happened", row["happened"]), ("expected", round(row["expected"], 2)),
                    ("luck", round(row["luck"], 2)),
                    ("score", round(row["luck"] / sigma, 2) if sigma else 0.0)]))

    luck_rows = []
    for player, pl in players.items():
        score = pl["net"] / math.sqrt(pl["var"]) if pl["var"] else 0.0
        row = OrderedDict([("player", player), ("score", round(score, 2)), ("percentile", round(_phi(score), 3)),
                           ("net", round(pl["net"], 2)), ("events", pl["events"])])
        for col in ("crit_net", "acc_net", "brn_net", "frz_net", "par_net", "flinch_net", "other_net", "deny_net"):
            row[col] = round(pl["nets"].get(col, 0.0), 2)
        row.update(crits_for=pl["cf"], crits_for_exp=round(pl["cfe"], 1),
                   crits_against=pl["ca"], crits_against_exp=round(pl["cae"], 1))
        luck_rows.append(row)
    luck_rows.sort(key=lambda r: -r["score"])

    moments.sort(key=lambda m: (m["odds"], m["order"]))
    moment_rows = []
    for n, m in enumerate(moments):
        row = OrderedDict(m["row"])
        row["id"] = "m%d" % n
        moment_rows.append(row)
    return {"luck": luck_rows, "luckdetail": detail_rows, "luckgames": game_rows, "luckmoments": moment_rows}
