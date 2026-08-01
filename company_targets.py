from __future__ import annotations


TOP_DEVICE_TARGETS: list[dict[str, object]] = [
    {
        "rank": 1,
        "key": "medtronic",
        "name": "Medtronic",
        "adapter": "planned",
        "company_patterns": ["%medtronic%", "%covidien%"],
        "revenue": "~$33B",
    },
    {
        "rank": 2,
        "key": "jnj",
        "name": "Johnson & Johnson MedTech",
        "adapter": "jnj",
        "company_patterns": [
            "%johnson & johnson%",
            "%johnson and johnson%",
            "%depuy%",
            "%ethicon%",
            # "%synthes%" also matches "Synthesis Health Intelligence Inc" (9 devices), an
            # unrelated company. The trailing space still matches "Synthes GmbH" and
            # "SYNTHES (U.S.A.) LP" while excluding it.
            "%synthes %",
            "%biosense webster%",
            "%cerenovus%",
            "%mentor worldwide%",
            "%mentor texas%",
            "%abiomed%",
            "%acclarent%",
        ],
        "revenue": "~$32B",
    },
    {
        "rank": 3,
        "key": "stryker",
        "name": "Stryker",
        "adapter": "stryker",
        "company_patterns": ["%stryker%", "%wright medical%"],
        "revenue": "~$22-23B",
    },
    {
        "rank": 4,
        "key": "siemens",
        "name": "Siemens Healthineers",
        "adapter": "planned",
        "company_patterns": ["%siemens%"],
        "revenue": "~$24-26B",
    },
    {
        "rank": 5,
        "key": "abbott",
        "name": "Abbott Laboratories",
        "adapter": "planned",
        "company_patterns": ["%abbott%", "%st. jude medical%", "%st jude medical%"],
        "revenue": "~$19-28B",
    },
    {
        "rank": 6,
        "key": "medline",
        "name": "Medline Industries",
        "adapter": "planned",
        "company_patterns": ["%medline%"],
        "revenue": "~$25B",
    },
    {
        "rank": 7,
        "key": "ge_healthcare",
        "name": "GE HealthCare",
        "adapter": "planned",
        # "%ge medical%" matched any company ending in "...ge Medical": Highridge Medical
        # (20,942 devices), Emerge Medical, Advantage Medical, NXStage Medical — ~24k devices
        # from unrelated makers counted as GE, which is most of what looked like a GE backlog.
        # Anchor on the real entity names instead.
        "company_patterns": ["ge healthcare%", "ge medical systems%", "ge vingmed%",
                             "ge ultrasound%", "%datex-ohmeda%"],
        "revenue": "~$19-20B",
    },
    {
        "rank": 8,
        "key": "philips",
        "name": "Philips",
        "adapter": "planned",
        "company_patterns": ["%philips%", "%respironics%"],
        "revenue": "~$19-20B",
    },
    {
        "rank": 9,
        "key": "boston_scientific",
        "name": "Boston Scientific",
        "adapter": "planned",
        "company_patterns": ["%boston scientific%"],
        "revenue": "~$16-17B",
    },
    {
        "rank": 10,
        "key": "bd",
        "name": "BD (Becton Dickinson)",
        "adapter": "planned",
        # "%bd%" matched Gembdi Dental (786) and ONE LAMBDA (425); "%bard%" matched
        # LOMBARD MEDICAL (668). Anchor on the real entity names.
        "company_patterns": ["%becton%", "bard %", "c. r. bard%", "c.r. bard%",
                             "%carefusion%"],
        "revenue": "~$15-16B",
    },
    {
        "rank": 11,
        "key": "cardinal_health",
        "name": "Cardinal Health",
        "adapter": "planned",
        "company_patterns": ["%cardinal health%"],
        "revenue": "~$12-13B",
    },
    {
        "rank": 12,
        "key": "baxter",
        "name": "Baxter",
        "adapter": "planned",
        "company_patterns": ["%baxter%", "%hill-rom%", "%hillrom%", "%welch allyn%"],
        "revenue": "~$10-12B",
    },
    {
        "rank": 13,
        "key": "edwards",
        "name": "Edwards Lifesciences",
        "adapter": "planned",
        "company_patterns": ["%edwards lifesciences%"],
        "revenue": "~$6-7B",
    },
    {
        "rank": 14,
        "key": "intuitive",
        "name": "Intuitive Surgical",
        "adapter": "planned",
        "company_patterns": ["%intuitive surgical%"],
        "revenue": "~$8-9B",
    },
    {
        "rank": 15,
        "key": "zimmer_biomet",
        "name": "Zimmer Biomet",
        "adapter": "planned",
        # "%biomet%" also matched Precision/Imaging/Bruin BIOMETRICS. Real Biomet
        # entities all start with the word, and "%zimmer%" still covers ZIMMER BIOMET INC.
        "company_patterns": ["%zimmer%", "biomet %"],
        "revenue": "~$7-8B",
    },
    {
        "rank": 16,
        "key": "olympus",
        "name": "Olympus",
        "adapter": "planned",
        "company_patterns": ["%olympus%", "%gyrus%"],
        "revenue": "~$6-7B",
    },
    {
        "rank": 17,
        "key": "b_braun",
        "name": "B. Braun",
        "adapter": "planned",
        "company_patterns": ["%b braun%", "%b. braun%", "%b.braun%", "%aesculap%"],
        "revenue": "~$9B",
    },
    {
        "rank": 18,
        "key": "smith_nephew",
        "name": "Smith+Nephew",
        "adapter": "planned",
        "company_patterns": ["%smith & nephew%", "%smith and nephew%", "%smith-nephew%", "%smith+nephew%"],
        "revenue": "~$5-6B",
    },
    {
        "rank": 19,
        "key": "dexcom",
        "name": "Dexcom",
        "adapter": "planned",
        "company_patterns": ["%dexcom%"],
        "revenue": "~$4B",
    },
    {
        "rank": 20,
        "key": "alcon",
        "name": "Alcon",
        "adapter": "planned",
        # NOT "%alcon%" — that also matches ALCONOX INC (98 devices), a laboratory-detergent
        # maker with nothing to do with Alcon. It is not merely noise: Alconox catalog 1104
        # collides with a real Alcon product 1104, so a loose pattern can attach an
        # eye-surgery IFU to a bottle of detergent.
        "company_patterns": ["%alcon laboratories%"],
        "revenue": "~$9-10B",
    },
    {
        "rank": 21,
        "key": "resmed",
        "name": "ResMed",
        "adapter": "planned",
        "company_patterns": ["%resmed%"],
        "revenue": "~$4-5B",
    },
    {
        "rank": 22,
        "key": "terumo",
        "name": "Terumo",
        "adapter": "planned",
        "company_patterns": ["%terumo%", "%microvention%"],
        "revenue": "~$6-7B",
    },
    {
        "rank": 23,
        "key": "fresenius",
        "name": "Fresenius Medical Care",
        "adapter": "planned",
        "company_patterns": ["%fresenius%"],
        "revenue": "~$5-6B",
    },
    {
        # Added on request 2026-07-16. Private (~$4-4.5B); a major sports-medicine /
        # arthroscopy / orthobiologics maker. Its SKUs were NOT in the original GUDID
        # seed — loaded separately via import_accessgudid.py --target arthrex.
        "rank": 19,
        "key": "arthrex",
        "name": "Arthrex",
        "adapter": "planned",
        "company_patterns": ["%arthrex%"],
        "revenue": "~$4-4.5B (private)",
    },
]


def target_by_key(key: str) -> dict[str, object]:
    normalized = key.strip().lower()
    for target in TOP_DEVICE_TARGETS:
        if target["key"] == normalized:
            return target
    raise KeyError(f"Unknown ChatIFU target: {key}")


def implemented_targets() -> list[str]:
    return [str(target["key"]) for target in TOP_DEVICE_TARGETS if target["adapter"] != "planned"]
