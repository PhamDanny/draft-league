"""Parse one Pokemon Showdown replay log into a per-game record."""
import re
from collections import defaultdict

# [from] tags that count as indirect damage, mapped to the names the dashboard shows
INDIRECT_NAMES = {
    "item: Life Orb": "Life Orb recoil",
    "Recoil": "Move recoil",
    "psn": "Poison",
    "brn": "Burn",
    "confusion": "Confusion",
}
SELF_KO_MOVES = {"Explosion", "Selfdestruct", "Memento", "Healing Wish", "Lunar Dance", "Final Gambit"}
WEATHER_NAMES = {"RainDance": "Rain", "SunnyDay": "Sun", "Sandstorm": "Sandstorm", "Hail": "Hail"}


def base_species(detail):
    """'Tornadus-Therian, M' -> 'Tornadus-Therian'. Cosmetic/battle-only formes fold into one name."""
    sp = detail.split(",")[0].strip()
    if sp == "Keldeo-Resolute":
        return "Keldeo"
    for fam in ("Genesect", "Arceus"):
        if sp.startswith(fam + "-"):
            return fam
    return sp


def hp_frac(s):
    """'254/282' -> 0.9007; '0 fnt' -> 0.0; '45/100 par' -> 0.45."""
    s = s.split(" ")[0]
    if s == "0":
        return 0.0
    cur, mx = s.split("/")
    return int(cur) / int(mx)


class Mon:
    def __init__(self, species):
        self.species = species
        self.frac = 1.0
        self.fainted = False
        self.brought = False
        self.led = False
        self.kos = 0
        self.damage = 0.0
        self.taken = 0.0
        self.turns = 0
        self.crits = 0
        self.super_effective = 0
        self.misses = 0
        self.statuses = 0
        self.friendly_fire = 0.0
        self.sacrifices = 0
        self.moves = defaultdict(lambda: {"uses": 0, "kos": 0, "damage": 0.0})
        self.on_field = False


def parse_replay(rep):
    log = rep["log"].split("\n")
    accounts = {}
    winner_acc = None
    mons = {"p1": {}, "p2": {}}  # side -> species -> Mon
    nick = {}  # "p1a: Mew" -> (side, species)
    slots = {}  # "p1a" -> species currently there
    turn = 0
    weather = []
    rosters = {"p1": [], "p2": []}
    killer = {}  # (side, species) -> side that dealt the fatal hit, until the |faint| line
    indirect = []  # (source, frac, fainted)
    events = []  # ordered raw event dicts, used by the luck pass
    faints = []  # (turn, side, species, killer_side)
    last = None  # (side, species, move) of the most recent move
    started = False
    lead_seen = {"p1": 0, "p2": 0}
    pending_crit = False

    def get(side_pos):
        side = side_pos[:2]
        key = side_pos
        if key in nick:
            return nick[key]
        return None

    i = 0
    while i < len(log):
        line = log[i]
        i += 1
        if not line.startswith("|"):
            continue
        p = line.split("|")
        t = p[1]
        if t == "player" and len(p) > 3 and p[3]:
            accounts[p[2]] = p[3]
        elif t == "poke":
            sp = base_species(p[3])
            if sp not in rosters[p[2]]:
                rosters[p[2]].append(sp)
        elif t == "win":
            winner_acc = p[2]
        elif t == "turn":
            turn = int(p[2])
            started = True
            for side in mons:
                for m in mons[side].values():
                    if m.on_field and not m.fainted:
                        m.turns += 1
        elif t in ("switch", "drag"):
            pos = p[2]
            side = pos[:2]
            sp = base_species(p[3])
            m = mons[side].setdefault(sp, Mon(sp))
            if not m.brought:
                m.brought = True
            # leads: first two switch-ins before turn 1
            if turn == 0 and lead_seen[side] < 2:
                m.led = True
                lead_seen[side] += 1
            nick[pos] = (side, sp)
            # whoever was in this slot is no longer on the field
            old = slots.get(pos[:3])
            if old and old in mons[side]:
                mons[side][old].on_field = False
            slots[pos[:3]] = sp
            m.on_field = True
            m.frac = hp_frac(p[4])
        elif t == "move":
            who = nick.get(p[2])
            if who:
                side, sp = who
                last = (side, sp, p[3])
                mons[side][sp].moves[p[3]]["uses"] += 1
                if p[3] in SELF_KO_MOVES:
                    mons[side][sp].sacrifices += 0  # counted on faint below
            events.append({"type": "move", "turn": turn, "side": side if who else None,
                           "mon": sp if who else None, "move": p[3], "target": p[4] if len(p) > 4 else "",
                           "raw": line})
        elif t == "-weather":
            if p[2] != "none" and not (len(p) > 3 and p[3] == "[upkeep]"):
                w = WEATHER_NAMES.get(p[2], p[2])
                if w not in weather:
                    weather.append(w)
        elif t == "-crit":
            who = nick.get(p[2])
            if last:
                mons[last[0]][last[1]].crits += 1
        elif t == "-supereffective":
            if last:
                mons[last[0]][last[1]].super_effective += 1
        elif t == "-miss":
            if last:
                mons[last[0]][last[1]].misses += 1
        elif t == "-status":
            who = nick.get(p[2])
            src = " ".join(p[3:])
            if who and last and "[from]" not in src and last[0] != who[0]:
                mons[last[0]][last[1]].statuses += 1
        elif t in ("-heal", "-sethp"):
            who = nick.get(p[2])
            if who:
                mons[who[0]][who[1]].frac = hp_frac(p[3])
        elif t == "-damage":
            who = nick.get(p[2])
            if not who:
                continue
            side, sp = who
            m = mons[side][sp]
            new = hp_frac(p[3])
            lost = max(0.0, m.frac - new)
            m.frac = new
            src = ""
            for extra in p[4:]:
                if extra.startswith("[from]"):
                    src = extra[6:].strip()
            fainted = p[3].endswith("fnt")
            if src:
                name = INDIRECT_NAMES.get(src)
                if name is None:
                    name = src[len("ability: "):] if src.startswith("ability: ") else (
                        src[len("item: "):] if src.startswith("item: ") else src)
                indirect.append((name, lost, fainted))
                events.append({"type": "indirect", "turn": turn, "side": side, "mon": sp,
                               "source": name, "frac": lost})
                if fainted:
                    m.fainted = True
                    killer[(side, sp)] = None
                continue
            m.taken += lost
            if last:
                a_side, a_sp, a_move = last
                am = mons[a_side][a_sp]
                if a_side == side:
                    am.friendly_fire += lost
                else:
                    am.damage += lost
                    am.moves[a_move]["damage"] += lost
                if fainted:
                    m.fainted = True
                    if a_side != side:
                        am.kos += 1
                        am.moves[a_move]["kos"] += 1
                    killer[(side, sp)] = a_side if a_side != side else None
            elif fainted:
                m.fainted = True
                killer[(side, sp)] = None
            events.append({"type": "damage", "turn": turn, "side": side, "mon": sp, "frac": lost,
                           "attacker": last})
        elif t == "faint":
            who = nick.get(p[2])
            if who:
                side, sp = who
                m = mons[side][sp]
                if not m.fainted:
                    # fainted without a -damage line (Explosion, Memento, Destiny Bond...)
                    m.fainted = True
                if last and last[0] == side and last[1] == sp and last[2] in SELF_KO_MOVES:
                    m.sacrifices += 1
                faints.append((turn, side, sp, killer.pop((side, sp), None)))
                m.on_field = False
                m.frac = 0.0

    # players: p1/p2 accounts; winner side
    winner_side = None
    for s, a in accounts.items():
        if a == winner_acc:
            winner_side = s
    return {
        "accounts": accounts,
        "winner_side": winner_side,
        "turns": turn,
        "mons": mons,
        "weather": weather,
        "indirect": indirect,
        "faints": faints,
        "events": events,
        "rosters": rosters,
    }
