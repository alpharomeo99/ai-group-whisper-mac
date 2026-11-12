"""Daily Batch Dialogue Engine.

Pre-generates organic multi-persona daily conversational arcs for groups once a day.
Dormant accounts connect on-demand at scheduled timestamps to type and send their message,
then disconnect immediately.

Live AI Orchestration triggers ONLY when an external human speaks in the group.
Bot-to-bot interactions are strictly governed by this daily batch schedule.
"""
import asyncio
import json
import logging
import math
import random
import re
import time
from datetime import datetime
try:
    import ai
except ImportError:
    from . import ai

log = logging.getLogger("whisper.batch")

COMMON_TYPOS = {
    "their": "thier",
    "there": "ther",
    "weight": "weihgt",
    "protein": "prtoein",
    "dosing": "dosign",
    "tomorrow": "tommorow",
    "because": "becasue",
    "definitely": "definately",
    "honestly": "honstly",
    "running": "runnign",
    "fatigue": "fatgiue",
    "sleep": "slepe",
    "morning": "mornign",
    "cardio": "cardoi",
    "about": "abotu",
}

def maybe_inject_typo_correction(text, candidate=None, chance=None):
    """Introduces realistic fast thumb typos with persona-specific texting fingerprint.
    - Clinical biohackers & veteran mentors type cleanly with virtually zero sloppy typos and never send asterisk bubble spam.
    - Dry deadpan observers leave occasional lowercase slips uncorrected.
    - Rapid-fire mobile posters (young powerlifters, grapplers) make occasional phone slips and sometimes send a quick *word bubble.
    """
    if not text or len(text) < 16 or " ||| " in text:
        return text

    archetype_key = ""
    c_tone = ""
    if candidate:
        dt = {}
        try:
            dt = json.loads(candidate.get("details") or "{}") if isinstance(candidate.get("details"), str) else (candidate.get("details") or {})
        except Exception:
            dt = {}
        archetype_key = str(dt.get("archetype") or dt.get("archetype_key") or "").lower()
        c_tone = str(dt.get("tone_and_vibe") or dt.get("role_in_group") or "").lower()

    if any(k in archetype_key or k in c_tone for k in ("clinical", "researcher", "veteran", "mentor")):
        effective_chance = 0.01 if chance is None else chance
        style = "clean"
    elif any(k in archetype_key or k in c_tone for k in ("dry", "cynical", "observer")):
        effective_chance = 0.04 if chance is None else chance
        style = "uncorrected"
    elif any(k in archetype_key or k in c_tone for k in ("powerlifter", "young", "grappler", "combat", "aesthetics")):
        effective_chance = 0.07 if chance is None else chance
        style = "fast_correction"
    else:
        effective_chance = 0.04 if chance is None else chance
        style = "standard"

    if random.random() > effective_chance:
        return text

    words = text.split()
    for idx, w in enumerate(words):
        clean_w = re.sub(r"[^\w]", "", w.lower())
        if clean_w in COMMON_TYPOS:
            typo_word = COMMON_TYPOS[clean_w]
            words[idx] = re.sub(clean_w, typo_word, w, flags=re.IGNORECASE)
            typo_text = " ".join(words)
            if style in ("uncorrected", "clean"):
                return typo_text
            elif style == "fast_correction":
                return typo_text + " ||| " + f"*{clean_w}"
            else:
                corr = f"*{clean_w}" if random.random() < 0.60 else f"*{clean_w} lol"
                return typo_text + " ||| " + corr
    return text

def format_relationship_graph(candidates):
    """Constructs dynamic relationship pairings and shared banter context between active group personas."""
    if len(candidates) < 2:
        return "Regular members who chat casually."
    names = [c.get("persona_name") or c.get("account_name") for c in candidates if c.get("persona_name") or c.get("account_name")]
    if len(names) < 2:
        return "Regular members who chat casually."
    dynamics = [
        "Gym buddies: train heavy together, banter about leg day suffering and PRs.",
        "Friendly skeptics: one asks for lab numbers and science when the other hypes a new protocol.",
        "Supp banter duo: constantly roast each other's supplement stacks and overthinking.",
        "Veteran & learner: one shares hard-earned lessons, the other asks practical everyday tips.",
        "Work vs gym contrast: one complains about their corporate desk job, the other works freelance/trades."
    ]
    pairs = []
    shuffled = list(names)
    random.seed(int(time.time() // 86400))
    random.shuffle(shuffled)
    for i in range(0, len(shuffled) - 1, 2):
        dyn = dynamics[(i // 2) % len(dynamics)]
        pairs.append(f"{shuffled[i]} & {shuffled[i+1]} ({dyn})")
    random.seed()
    return "; ".join(pairs) if pairs else "Close community regulars who know each other's routines."



# ======================================================================
# COMPREHENSIVE 3-SESSION CONVERSATIONAL THEME REPOSITORY (270 TOTAL THEMES)
# ======================================================================
# Session 1: Peptides, Compounds, TRT, Bloodwork & Recovery Protocols (90 themes)
SESSION_1_THEMES = [   (   'Tendon & joint healing: BPC-157 vs TB-500 experiences, golfers elbow, '
        'rotator cuff flare-ups, recovery timelines.',
        'bpc tb500 healing tendon joint elbow'),
    (   'Pinning soreness PIP realities: virgin muscle pip, quad pinning '
        'regrets vs smooth ventroglute, warming vials.',
        'pip injection pain ventroglute quad delt soreness'),
    (   'Testosterone base optimization: 120-200mg/wk cruise vs blast, '
        'microdosing daily vs 2x/wk, steady serum levels.',
        'trt test cyp enan cruise blast frequency'),
    (   'Managing estrogen without crashing E2: puffy nipples, water '
        'retention, why crashing E2 with too much Arimidex feels like death.',
        'estrogen e2 arimidex aromasin sides nipple ai crash'),
    (   'Bloodwork deep-dive: analyzing liver AST/ALT, kidney eGFR, lipid '
        'panels (HDL/LDL), and when to worry vs stay chill.',
        'bloodwork labs ast alt hdl ldl lipids kidneys'),
    (   'Tirzepatide & Retatrutide cutting: food noise going completely dead '
        'silent, nausea management, high protein intake to preserve muscle.',
        'tirz reta sema glp1 food noise cut appetite'),
    (   'Growth hormone secretagogues: CJC-1295 / Ipamorelin vs MK-677, vivid '
        'REM dreams, morning water retention, tingling hands.',
        'cjc ipam mk677 gh dreams sleep growth'),
    (   'Skin, hair & anti-aging peptides: GHK-Cu copper peptide injections vs '
        'topicals, sting management, dilution with BAC water.',
        'ghk copper epithalon skin hair antiaging'),
    (   'Reconstitution chemistry & gear prep: BAC water ratios, insulin '
        'syringe gauge (29g vs 31g), fridge storage, preventing degradation.',
        'recon bac water insulin syringe storage fridge math'),
    (   'Peptides while traveling: flying through airport security with '
        'pre-mixed pens, cooling travel cases, TSA experiences.',
        'travel airport tsa cooling pen flight carryon'),
    (   'Appetite ramp-ups for hardgainers: MK-677 hunger spikes, waking up at '
        '3am raiding the fridge, managing insulin sensitivity.',
        'mk677 hunger appetite bulk fridge glucose'),
    (   'Post-injury rehab milestones: combining physiotherapy, eccentrics, '
        'and tissue-repair peptides to get back under heavy barbells.',
        'rehab injury physio squat bench recovery'),
    (   'Subq vs shallow IM injection debate: absorption rates, '
        'lumps/bruising, needle anxiety vs smooth pin routines.',
        'subq im injection pin needle bruise site'),
    (   'High hematocrit & RBC management: whole blood donation vs therapeutic '
        'phlebotomy, staying hydrated, cardio for blood viscosity.',
        'hematocrit rbc blood donation hydration viscosity trt'),
    (   'Hair loss & DHT sensitivity: finasteride, dutasteride, topical '
        'minoxidil, RU58841, deciding whether hair or delts matter more.',
        'hair shedding dht finasteride minoxidil baldness delts'),
    (   'Cardiovascular protection on cycle: daily fish oil, citrus bergamot, '
        'CoQ10, telmisartan, monitoring resting heart rate and BP.',
        'cardio heart blood pressure citrus bergamot bp fish oil'),
    (   'Acne & skin flare-ups: backne breakout solutions, salicylic acid '
        'washes, benzoyl peroxide, when people resort to low-dose accutane.',
        'acne backne skin breakout accutane wash clear'),
    (   'Blast & Cruise vs cycling off (PCT): why modern lifters abandon PCT, '
        'the mental crash of coming off, committing to test for life.',
        'blast cruise pct nolva clomid crash cruise off'),
    (   'Oral compounds debate: Anavar vs Turinabol vs Dianabol, pump back '
        'pumps, liver toxicity, why orals are overrated vs injectables.',
        'anavar tbol dbol orals pumps liver toxic'),
    (   'Masteron vs Primobolan: cosmetic dry look, anti-estrogenic feeling, '
        'DHT fullness, dealing with counterfeit primo.',
        'primo masteron mast dry cosmetic dht fake'),
    (   'First cycle regrets & mistakes: running too much too early, not '
        'having bloods done first, listening to bro-science at local gyms.',
        'first cycle mistake regret bloodwork broscience beginner'),
    (   'Sleep apnea & heavy lifter breathing: thick necks, snoring, CPAP '
        'machines changing life quality and daytime fatigue.',
        'sleep apnea cpap snore breathing neck fatigue'),
    (   'RAD-140 (Testolone) experiences: aggressive gym drive, dry '
        'vascularity, joint stiffness, suppression timeline.',
        'rad140 testolone strength dry aggression joints'),
    (   'LGD-4033 (Ligandrol) fullness vs water bloat: crazy glycogen storage, '
        'strength increases, post-cycle fatigue.',
        'lgd4033 ligandrol pumps bulk water bloat'),
    (   'Ostarine (MK-2866) realistic expectations: mild recomp, saving muscle '
        'in a steep calorie deficit, joint soothing.',
        'ostarine mk2866 cut recomp joints mild'),
    (   'Cardarine (GW-501516) unlimited cardio engine: dropping 5k run times, '
        'stamina, the infamous rodent study debate.',
        'cardarine gw501516 cardio stamina endurance 5k'),
    (   'Enclomiphene as a test base: keeping LH and FSH alive during research '
        'chemical runs, eye floaters risk, source purity.',
        'enclomiphene enclo test base lh fsh floaters'),
    (   'BPC-157 oral arginate vs subq acetate: gut healing, IBS flare-ups, '
        'systemic bioavailability differences.',
        'bpc arginate acetate oral subq gut ibs bioavailability'),
    (   'TB-500 systemic migration vs local site pinning: muscle belly tears, '
        'hamstring strains, tissue remodeling.',
        'tb500 systemic migration local pin muscle tear hamstring strain'),
    (   'GHK-Cu injection burn solutions: dilution ratios with BAC water, '
        'combining with BPC-157 in the same barrel to kill sting.',
        'ghk copper sting burn dilute bac water bpc barrel combo'),
    (   'Tesamorelin vs Ipamorelin for visceral fat loss: belly fat reduction, '
        'IGF-1 levels, nighttime administration timing.',
        'tesamorelin ipam visceral fat igf1 night dosing'),
    (   'Prolactin flare-ups on 19-nors: puffy sensitive nipples without high '
        'E2, P5P vs Cabergoline, mood changes.',
        'prolactin p5p caber cabergoline 19nor deca npp sides'),
    (   'Nandrolone Decanoate (Deca) vs NPP (Phenylpropionate): joint '
        'lubrication benefits vs Deca dick and mental fog.',
        'deca npp nandrolone joint lubrication deca dick mental'),
    (   'Equipoise (Boldenone) endurance vs high hematocrit: insane '
        'vascularity, slow ester clearance, anxiety on EQ.',
        'equipoise boldenone eq vascularity rbc anxiety hematocrit'),
    (   'Winstrol (Stanozolol) dry aesthetics vs joint pain: dry graininess '
        'before beach trips, brittle knees, fish oil dosing.',
        'winstrol winny dry joints knees brittle aesthetics cut'),
    (   'Clenbuterol fat burner shakes: hand tremors, taurine supplementation '
        'to stop muscle cramps, heart rate spikes.',
        'clen clenbuterol shakes tremors taurine cramps fat loss'),
    (   'T3 (Cytomel) thyroid speedups: flat muscles from glycogen depletion, '
        'pairing T3 with anabolics to prevent muscle loss.',
        't3 cytomel thyroid metabolism flat muscle catabolism'),
    (   'HCG on TRT: preserving testicular size, fertility protocols, '
        'intratesticular testosterone, managing E2 spikes from HCG.',
        'hcg fertility balls size trt estrogen aromatase'),
    (   'Needle gauge selection: 27g vs 29g vs 31g insulin pins for shallow IM '
        'delts, minimizing scar tissue over years of pinning.',
        'needle gauge pin 27g 29g 31g insulin delt scar tissue'),
    (   'Drawing thick oil: warming vials in warm water, 21g drawing pin vs '
        '25g/27g injection pin, drawing air first.',
        'drawing oil needle warm vial 21g 25g slow draw air pin'),
    (   'Ventroglute mapping: finding the exact pocket between hip bone and '
        'iliac crest, zero pain, why it beats quads 100 to 1.',
        'ventroglute hip injection site glute map quad pip painless'),
    (   'Delt pinning technique: locating middle delt, 0.5ml-1ml max volume, '
        'avoiding the circumflex nerve.',
        'delt shoulder pin injection volume needle length ml site'),
    (   'Lats and pecs pinning: advanced injection sites, terrifying virgin '
        'muscle PIP, why most lifters stick to glutes and delts.',
        'lats pecs pin chest site injection pip advanced'),
    (   'Aromasin (Exemestane) suicidal aromatase inhibitor: why it prevents '
        'estrogen rebound vs Arimidex, dosing 6.25mg-12.5mg.',
        'aromasin exemestane ai suicidal inhibitor e2 rebound arimidex'),
    (   'Crashing E2 horror stories: dry clicking joints, erectile '
        'dysfunction, zero libido, severe depression, recovering with HCG or '
        'Dbol.',
        'crashed e2 low estrogen joint dry libido depression recover dbol'),
    (   'High E2 symptoms: emotional outbursts, crying at movies, oily '
        'forehead, morning wood vanishing, water bloat on ankles.',
        'high e2 symptoms emotional water bloat face nipples wood'),
    (   'Liver toxicity support stacks: TUDCA vs NAC vs Milk Thistle, taking '
        'TUDCA 2 hours away from orals to avoid liver stress.',
        'tudca nac liver support enzymes ast alt toxicity orals'),
    (   'Kidney health on gear: eGFR drops from high muscle mass and creatine '
        'vs real kidney damage, cystatin-C testing.',
        'kidney egfr cystatin c creatinine hydration bp damage renal'),
    (   'Blood pressure management on blast: Telmisartan (Micardis) 20-40mg, '
        'Nebivolol for heart rate, avoiding left ventricular hypertrophy.',
        'telmisartan nebivolol bp blood pressure heart lvh cardio gear'),
    (   'Lipid profile fixes: Citrus Bergamot 1000mg, Ezetimibe, Red Yeast '
        'Rice, omega 3, rescuing HDL crushed by orals.',
        'citrus bergamot ezetimibe lipids hdl ldl cholesterol statin orals'),
    (   'Melanotan 2 (MT2) tanning protocols: acute nausea on first pin, '
        'random facial freckles, avoiding dark moles, sun exposure.',
        'melanotan mt2 tan skin nausea freckles moles sun libido'),
    (   'PT-141 (Bremelanotide) aphrodisiac peptide: crazy libido spikes, '
        'nausea window, dosing 4 hours before date night.',
        'pt141 bremelanotide libido erection sex nausea date night'),
    (   'Epithalon (Epitalon) longevity protocol: 10-day cycles, pineal gland '
        'reset, deep restorative delta sleep.',
        'epithalon epitalon telomere pineal melatonin sleep longevity'),
    (   'MOTS-c mitochondrial peptide: morning fasted cardio administration, '
        'fat oxidation, endurance capacity.',
        'motsc mitochondria fasted cardio fat oxidation endurance energy'),
    (   'SS-31 mitochondrial repair: cellular ATP restoration, chronic fatigue '
        'relief, tendon recovery.',
        'ss31 mitochondria atp fatigue recovery tendon cellular'),
    (   'Glutathione injectable antioxidant: skin brightening, liver detox, '
        'sulfur smell after pin, combining with vitamin C.',
        'glutathione antioxidant liver detox skin sulfur pin injectable'),
    (   'L-Carnitine injectable: upgrading androgen receptor density, fishy '
        'TMAO concerns, taking garlic extract, pinning pre-workout.',
        'carnitine injectable ar androgen receptor preworkout tmao pumps'),
    (   'BPC-157 for severe gut issues: healing acid reflux, ulcerative '
        'colitis flare-ups, taking oral caps with food.',
        'bpc gut ibs acid reflux stomach healing oral caps'),
    (   'TB-500 loading vs maintenance: 2mg twice a week for 4 weeks then 2mg '
        'bi-weekly, flexibility and systemic recovery.',
        'tb500 dose loading protocol maintenance flexibility recovery tissue'),
    (   'Knee joint cartilage wear: combining BPC-157, Pentosan Polysulfate '
        '(PPS), and hyaluronic acid for meniscus support.',
        'knee meniscus cartilage pps pentosan bpc joint wear osteoarthritis'),
    (   'Bicep tendonitis relief: eccentric hammer curls, deep tissue '
        'scraping, BPC local subcutaneous injections.',
        'bicep tendon tendonitis hammer curl scraping bpc pin local'),
    (   'Achilles tendon tightness: eccentric heel drops on stairs, TB-500 '
        'recovery, avoiding heavy calf raises during flare-ups.',
        'achilles tendon heel drop calf tb500 running mobility'),
    (   'Lower back pumps on deadlifts: Taurine 3g-5g daily, potassium, '
        'hydrating with electrolytes to relieve crippling back stiffness.',
        'back pumps deadlift taurine electrolytes potassium stiffness orals'),
    (   'Sleep quality on Trenbolone: night sweats soaking bedsheets, '
        'Trenbolone insomnia, waking up at 2am with raging heartbeat.',
        'tren night sweats insomnia sleep sheet soak heart rate paranoia'),
    (   'Mental sides on 19-nors: relationship paranoia, irrational jealousy, '
        'dopamine downregulation, managing mental health on cycle.',
        'tren deca mental sides paranoia jealousy relationship dopamine mood'),
    (   'Anavar pump cramps: forearm pumps so tight you cant hold a steering '
        'wheel, Shin pumps while walking up hills.',
        'anavar pumps forearm shin cramps walking steering wheel tightness'),
    (   'Masteron mood enhancement: feeling like a Greek god, clean '
        'confidence, androgenic drive without estrogen water bloat.',
        'masteron mood confidence drive dry hardness cosmetic aesthetic'),
    (   'Primobolan source authenticity: why 80% of cheap Primo is just Test '
        'or Masteron, lab testing with Janoshik.',
        'primo test fake source janoshik purity coa underground lab'),
    (   'Sustanon 250 vs single ester Test Cypionate: hormone rollercoaster '
        'from short prop ester vs flat stable levels on Cyp.',
        'sustanon test cyp prop ester rollercoaster peak trough stability'),
    (   'Subcutaneous TRT vs IM TRT: smoother aromatization curve, zero scar '
        'tissue, dealing with small itchy lumps under skin.',
        'subq im trt aromatization lumps scar tissue insulin pin daily'),
    (   'Donating whole blood risks: crashing ferritin levels into single '
        'digits, extreme fatigue, checking ferritin before phlebotomy.',
        'blood donation ferritin iron crash fatigue rbc hematocrit phlebotomy'),
    (   'Resting heart rate spikes on cycle: resting HR going from 55 to 78, '
        'daily Zone 2 cardio on incline treadmill to bring it down.',
        'resting heart rate rhr cardio zone2 treadmill blast health'),
    (   'Insulin pins for peptide mixing: why 0.3ml and 0.5ml 31g syringes '
        'give 5x better dosing accuracy than 1ml barrels.',
        'insulin pin 31g 03ml 05ml syringe accuracy math peptide recon'),
    (   'BAC water shelf life: cloudy vials, benzyl alcohol degradation, '
        'keeping open BAC bottles in dark fridge drawers.',
        'bac water shelf life cloudy sterile benzyl alcohol fridge storage'),
    (   'Lyophilized peptide storage: freezer powder stability for 2 years vs '
        'reconstituted liquid degrading after 4-6 weeks.',
        'lyophilized powder storage freezer stability fridge degrade weeks'),
    (   'Sublingual peptides myth: why oral swallow vs under tongue matters, '
        'gastric breakdown without arginate salt.',
        'sublingual oral peptide myth stomach acid arginate absorption'),
    (   'Morning fasted cardio on GLP-1s: keeping blood sugar stable, drinking '
        'electrolytes, avoiding dizzy spells.',
        'fasted cardio glp1 hypoglycemia dizziness electrolytes morning '
        'routine'),
    (   'Satiety vs nausea on Tirzepatide: sweet spot between zero cravings '
        'and feeling sick, why force-feeding protein is needed.',
        'tirzepatide dose sweet spot nausea cravings protein shake'),
    (   'Preserving glutes & delts on GLP-1 cut: heavy lifting volume, '
        'creatine, maintaining strength numbers while weight drops.',
        'glp1 cut preserve muscle strength creatine volume glutes delts'),
    (   'Retatrutide triple agonist: glucagon receptor boosting thermogenesis, '
        'heart rate increases, the future of cutting peptides.',
        'retatrutide reta glucagon thermogenesis heart rate cutting fat loss'),
    (   'Cagrilintide amylin analogue: pairing with Semaglutide for extreme '
        'appetite control, feeling full on 3 bites of food.',
        'cagrilintide cagri sema amylin satiety hunger full bites'),
    (   'Microdosing GLP-1s for maintenance: 1.25mg Tirzepatide every 10 days '
        'to keep food noise quiet without losing more weight.',
        'microdose glp1 maintenance tirzepatide weight stability food noise'),
    (   'Rebound hunger after stopping GLP-1s: preparing meal discipline, '
        'keeping fiber and volume high so fat doesnt snap back.',
        'glp1 rebound hunger stop diet volume fiber discipline regain'),
    (   'Steroid-induced gynecomastia (gyno) surgery: glandular excision vs '
        'liposuction, recovery timelines, preventing scar tissue.',
        'gyno surgery excision gland lipo recovery scar tissue raloxifene'),
    (   'Raloxifene for gyno reversal: 60mg daily for 6-8 weeks to shrink '
        'established lumps under nipples, SERM effectiveness.',
        'raloxifene gyno lump nipple shrink serm tamoxifen reversal'),
    (   'Tamoxifen (Nolvadex) vs Clomid for PCT: why Clomid causes emotional '
        'mood swings and floaters while Nolva is cleaner.',
        'nolvadex clomid pct serm mood swings floaters emotional recovery'),
    (   'HPTA restart timeline: how long testicle signaling actually takes '
        'after 12 weeks of suppression, realistic recovery windows.',
        'hpta restart testicle signaling lh fsh recovery months suppression'),
    (   'Cruising on 150mg Test vs going natural: weighing lifelong pins '
        'against hormone stability and high quality of life.',
        'cruise trt natural lifestyle lifelong injections quality life '
        'hormones'),
    (   'Bloodwork timing: why drawing blood at peak vs trough matters, '
        'fasting 12 hours before test, avoiding heavy workout day before.',
        'bloods timing peak trough fasting liver workout ast alt skew'),
    (   'High SHBG lifters: why total test looks high while free test is '
        'crushed, using Boron 10mg or low dose Proviron to free up test.',
        'shbg free test boron proviron binding total test low libido'),
    (   'Low SHBG lifters: rapid testosterone clearance, daily pinning '
        'schedules, dealing with rapid aromatization.',
        'low shbg daily pinning microdose clearance aromatization e2 spikes'),
    (   'Proviron (Mesterolone) libido polish: DHT derivative, hardening '
        'effect, vascularity, mood elevation on TRT.',
        'proviron mesterolone dht libido hardness vascularity mood'),
    (   'Masteron vs hair genetics: if you are prone to male pattern baldness, '
        'DHT compounds will accelerate it in weeks.',
        'masteron dht hair baldness mpb genetics shedding crown temples'),
    (   'Ketoconazole (Nizoral) shampoo: 2% shampoo leave-on for 5 minutes, '
        'washing away scalp sebum and DHT buildup.',
        'nizoral ketoconazole shampoo scalp dht shedding hair protection'),
    (   'Microneedling scalp for hair regrowth: 1.5mm dermaroller once a week '
        'paired with topical minoxidil, real hair results.',
        'microneedling dermaroller scalp minoxidil hair growth follicles'),
    (   'Pre-exhausting delts before heavy overhead press: lateral raises '
        'first, sparing shoulder rotator cuffs from heavy load.',
        'pre exhaust delts lateral raise ohp shoulder health press'),
    (   'Incline dumbbell press vs flat barbell bench: deeper stretch on '
        'clavicular pec, lower shoulder impingement risk.',
        'incline dumbbell bench pec stretch shoulder impingement chest'),
    (   'Squat heel elevation: squatted in Olympic lifting shoes or wedges, '
        'quad quad isolation, fixing poor ankle mobility.',
        'squat shoes wedges heel elevation quad mobility ankle depth'),
    (   'Romanian deadlifts (RDLs) for hamstring growth: hip hinge mechanics, '
        'feeling the hamstring stretch, avoiding lumbar flexion.',
        'rdl romanian deadlift hamstring glute stretch hinge back safe'),
    (   'Hip thrust pad bruising: using thick barbell pads or mats, locking '
        'out glutes at the top without hyperextending back.',
        'hip thrust pad bruise barbell lockout glute hypertrophy form'),
    (   'Wrist wraps vs lifting straps: when to train raw grip vs when straps '
        'are essential to overload lats on heavy rows.',
        'straps wrist wraps grip strength lats deadlift overload back'),
    (   'Cold plunge after lifting debate: why ice baths blunt hypertrophy if '
        'done within 4 hours of resistance training.',
        'cold plunge ice bath hypertrophy blunt inflammation recovery window'),
    (   'Sauna post-workout: heat shock proteins, blood flow to sore joints, '
        'relaxation, drinking 1L water with sodium after.',
        'sauna heat shock protein recovery joints relaxation hydration salt'),
    (   'Electrolyte balance on low carbs: 4g-5g sodium, potassium, magnesium '
        'glycinate, curing keto gym headaches.',
        'electrolytes sodium salt potassium magnesium headache low carb gym')]

SESSION_2_THEMES = [   (   'Corporate Slack drama: passive-aggressive thumbs up emojis from '
        'middle managers, avoiding workplace drama.',
        'slack corporate drama office manager passive aggressive emoji'),
    (   '8am Zoom calls after brutal leg day: sitting on a hard office chair '
        'when quads and glutes are completely on fire.',
        'zoom meeting leg day sore chair corporate morning cameras'),
    (   'Remote work vs office grind: sneaking a 45-minute gym session at '
        'lunch vs sitting in bumper-to-bumper commute traffic.',
        'remote work office wfh lunch workout commute traffic routine'),
    (   'Standing desks and back pain: switching between sitting and standing, '
        'ergonomic chairs, relieving lower back tightness.',
        'standing desk back pain posture ergonomics office work sitting'),
    (   'Meal prep disasters: glass Tupperware exploding in gym bag, rice and '
        'ground beef covering clean lifting shirts.',
        'tupperware glass explode meal prep gym bag rice beef disaster'),
    (   'Ground beef and jasmine rice fatigue: eating 1lb beef daily for 4 '
        'months, trying different hot sauces to make it tolerable.',
        'ground beef rice food fatigue hot sauce bulk seasoning meal prep'),
    (   'Ninja Creami protein ice cream: xantham gum, fairlife milk, whey '
        'isolate, creating 300-calorie pints to survive a cut.',
        'ninja creami protein ice cream fairlife xanthan whey volume cut'),
    (   'Dating someone who doesn’t lift: them asking why your fridge has 18 '
        'meal prep containers and zero beer.',
        'dating non lifter fridge meal prep tupperware dinner awkward'),
    (   'Gym selfies on dating profiles: whether bathroom mirror flex pics '
        'help or completely scare away normal girls.',
        'dating apps tinder hinge gym selfie mirror flex cringe bio'),
    (   'Awkward first dates at restaurants: trying to guess the calories in '
        'restaurant steak while your date orders cocktails.',
        'first date restaurant menu calories steak tracking macros awkward'),
    (   'Skipping alcohol on night outs: ordering club soda with lime to avoid '
        'questions, waking up at 7am with zero hangover.',
        'alcohol night out club soda lime sober gym morning hangover'),
    (   'Gym character: the guy curling in the squat rack with an empty '
        'barbell while 4 people are waiting to squat.',
        'curling squat rack empty bar waiting gym pet peeve character'),
    (   'Gym character: teenage tripod armies filming TikToks: setting up 3 '
        'phone tripods in the dumbbell section for 45 mins.',
        'tripod tiktok teens gym camera filming dumbbell path block'),
    (   'Gym character: the screamer on leg press: loading 3 plates and '
        'screaming like he is lifting a car, quarter reps.',
        'leg press screamer plates quarter reps noise gym character'),
    (   'Commercial gym music atrocities: 2014 EDM pop remixes played at '
        'ear-splitting volume, noise-cancelling headphones dying.',
        'gym music playlist pop edm airpods dead battery noise cancel'),
    (   'Forgetting lifting straps on heavy back day: forearms burning out on '
        'set 2 of bent-over rows, grip failing early.',
        'forgot straps back day grip forearm burn heavy rows gym fail'),
    (   'Exploded shaker cup in hot car: leaving whey protein in the cup under '
        'the summer sun for 3 days, biological hazard smell.',
        'shaker cup smell hot car whey protein rotten biological hazard wash'),
    (   'Weekend grocery haul: spending $250 on chicken breast, eggs, Greek '
        'yogurt, and berries, cashier looking terrified.',
        'groceries haul chicken eggs greek yogurt berries cost bulk food'),
    (   'Costco gym essentials: 5lb bags of frozen wild blueberries, egg '
        'whites by the case, giant flank steak packs.',
        'costco gym haul egg whites blueberries flank steak protein savings'),
    (   'Air fryer meal prep speedruns: cooking 2lbs of diced chicken thighs '
        'in 14 minutes with zero mess, seasoning blends.',
        'air fryer chicken thighs meal prep fast cook crispy seasoning'),
    (   'Greek yogurt late night snack: mixing chocolate whey into Fage 0% '
        'Greek yogurt for protein pudding, zero cravings.',
        'greek yogurt fage whey pudding late night snack sweet craving'),
    (   'Hydration gallon jug habit: carrying a giant water jug everywhere, '
        'getting weird looks in office boardroom meetings.',
        'gallon jug water hydration office meeting bathroom breaks desk'),
    (   'Office coffee machine abuse: drinking 4 espressos before noon, '
        'afternoon jitters, switching to decaf after 1pm.',
        'coffee espresso caffeine office jitters afternoon crash decaf'),
    (   'Commuting after heavy workout: manual transmission car when your left '
        'calf and clutch foot are cramping up.',
        'commute manual car clutch calf cramp leg day driving traffic'),
    (   'Gym clothes laundry mountain: washing 6 gym shirts and knee sleeves '
        'every 3 days, athletic fabric detergent.',
        'laundry gym clothes knee sleeves smell athletic wash detergent sweat'),
    (   'Knee sleeve funk: washing neoprene sleeves in vinegar to kill the '
        'sweat bacteria smell that survives regular soap.',
        'knee sleeves smell neoprene vinegar wash bacteria stink gym gear'),
    (   'Gym buddy flaking on leg day: getting the cancel text at 5:45am, '
        'lifting heavy solo.',
        'gym buddy flake leg day text solo workout morning discipline'),
    (   'Home gym vs commercial gym: lifting in a cold garage in winter vs '
        'having 8 different cable machines and sauna.',
        'home gym garage cold winter commercial cables machines sauna pros '
        'cons'),
    (   'Work deadlines killing gym motivation: pushing through a 45-minute '
        'upper body session at 8pm just to keep consistency.',
        'work deadline tired 8pm evening gym consistency mental discipline'),
    (   'Boss scheduling Friday 4:30pm meetings: the rage of watching your '
        'pre-workout kick in while stuck in a pointless Zoom.',
        'preworkout meeting zoom friday boss scheduling rage focus wasted'),
    (   'Gym crush eye contact: making awkward 1-second eye contact across the '
        'cable crossovers, focusing back on your set.',
        'gym crush eye contact cables workout awkward focus lift'),
    (   'Gym pet peeve: leaving dumbbells scattered: 85lb dumbbells left in '
        'front of the rack, tripping hazards.',
        'rerack weights dumbbells floor gym etiquette pet peeve laziness'),
    (   'Gym pet peeve: sweating on the bench and walking away: leaving a '
        'human silhouette of sweat without wiping it down.',
        'wipe bench sweat towel gym etiquette hygiene spray paper towel'),
    (   'Sweet potato fries air fryer hack: tossing in avocado oil spray and '
        'cinnamon, post-workout carb reload.',
        'sweet potato fries air fryer post workout carbs cinnamon avocado oil'),
    (   'Rice cooker game changer: rinsing jasmine rice 4 times, using bone '
        'broth instead of water for extra flavor and protein.',
        'rice cooker jasmine bone broth flavor carbs easy digestion prep'),
    (   'Liquid egg white chugging: whether anyone actually drinks pasteurized '
        'egg whites straight from the carton.',
        'egg whites liquid drink carton chug raw pasteurized protein fast'),
    (   'Whey protein digestion bloat: switching from whey concentrate to whey '
        'isolate or beef protein to stop bloating.',
        'whey concentrate isolate bloat digestion stomach gas beef protein'),
    (   'Lifting on an empty stomach: morning fasted lifting vs having cream '
        'of rice and honey 45 mins before training.',
        'fasted lifting cream of rice honey pre workout meal pumps energy'),
    (   'Cream of rice with peanut butter: the elite pre-workout carb source '
        'that digests in 20 minutes with zero stomach heaviness.',
        'cream of rice peanut butter carbs pre workout digest fast energy'),
    (   'Salt before training: adding half a teaspoon of pink Himalayan salt '
        'to pre-workout for skin-splitting pumps.',
        'salt pre workout pump pink himalayan sodium hydration vascular'),
    (   'Dating app red flags: girls whose bio says fluent in sarcasm vs guys '
        'posing in front of rented sports cars.',
        'dating apps hinge red flags bio sarcasm gym lifestyle match'),
    (   'Balancing social life and lifting: leaving the bar at 11pm to get 8 '
        'hours of sleep, friends giving you hard times.',
        'social life bar friends leaving early sleep recovery discipline '
        'balance'),
    (   'Holiday family dinners: relatives asking if you are still doing that '
        'diet thing while examining your plate.',
        'family dinner holidays diet questions relatives food comments plate'),
    (   'Vacation gym hunting: looking for hardcore bodybuilding gyms in other '
        'cities with day passes instead of hotel gyms.',
        'travel vacation gym day pass hardcore dumbbells hotel gym search'),
    (   'Hotel gym horrors: two 15lb dumbbells, a broken treadmill, and a yoga '
        'ball from 2004.',
        'hotel gym dumbbells broken treadmill travel workout struggle'),
    (   'Buying dress clothes as a lifter: shirts that fit the delts and chest '
        'billowing like parachutes around the waist.',
        'dress shirts suits lifter tailoring broad shoulders waist taper '
        'clothes'),
    (   'Jeans shopping with big quads: thighs completely painted on while '
        'waist has a 4-inch gap, stretch denim salvation.',
        'jeans quads big legs stretch denim waist gap clothes shopping'),
    (   'Lifting shoes vs bare feet: deadlifting in socks or flat wrestling '
        'shoes vs running shoes with squishy air soles.',
        'shoes deadlift socks wrestling flat sole cushion stability form'),
    (   'Gym bag inventory: belt, knee sleeves, liquid chalk, wraps, bands, '
        'smelling salts, weighing 25 lbs.',
        'gym bag belt sleeves wraps chalk bands heavy gear essentials'),
    (   'Liquid chalk vs dry chalk: commercial gyms banning chalk, sneaking '
        'liquid chalk that dries in 10 seconds with no dust.',
        'liquid chalk dry gym ban grip deadlift mess sneak hands'),
    (   'Earbuds falling out during heavy sets: mid-squat cable snag or sweaty '
        'ear tips popping out during a top set.',
        'earbuds fall out airpods sweat bench squat heavy set music lost'),
    (   'Foam rolling: whether spending 20 minutes rolling IT bands actually '
        'does anything or if active warmups beat it.',
        'foam roll warmup it band mobility stretch active warmup time'),
    (   'Morning mobility routine: couch stretch, 90/90 hips, world greatest '
        'stretch, opening up hips before squatting.',
        'mobility routine hips couch stretch 9090 squat warmup movement'),
    (   'Deload week mental challenge: lifting 50% lighter and feeling like '
        'you are wasting time, trusting the systemic recovery.',
        'deload week light mental fatigue nervous system joint recovery trust'),
    (   'Overtraining vs under-recovering: realizing your fatigue is just from '
        '5 hours of sleep and eating 1800 calories.',
        'overtraining sleep food recovery fatigue calories stress burnout'),
    (   'Walking 10k steps a day: how daily low-intensity walking drops fat '
        'without making you ravenously hungry.',
        'steps 10k walking neat fat loss low intensity recovery cardio'),
    (   'Desk posture stretches: chin tucks, doorway pec stretch, countering 8 '
        'hours of hunching over a laptop.',
        'posture desk stretch doorway pec chin tuck neck hunch laptop'),
    (   'Blue light glasses at night: placebo vs actually falling asleep '
        'faster when looking at computer screens after 9pm.',
        'blue light glasses screens sleep evening melatonin eye strain'),
    (   'Blackout curtains & bedroom cold temp: setting AC to 66 degrees, '
        'total dark, waking up completely refreshed.',
        'blackout curtains sleep bedroom cold temp deep rem recovery'),
    (   'White noise machines vs silence: brown noise, fan humming, blocking '
        'out city street sounds for deep sleep.',
        'white noise brown sound fan sleep city noise block deep sleep'),
    (   'Weekend cheat meals turning into all-day binges: eating a whole pizza '
        'and wake up 6 lbs heavier from water.',
        'cheat meal binge pizza water weight scale spike morning bloat'),
    (   'Scale weight fluctuations on a cut: scale jumping 3lbs after eating '
        'high sodium dinner, trusting the weekly average.',
        'scale weight daily fluctuate sodium water weekly average cut panic'),
    (   'Taking progress photos: same lighting, same time morning, why '
        'bathroom mirror lighting lies to you.',
        'progress photos lighting morning mirror scale bodyfat tracking truth'),
    (   'Cooking meal prep for the week: 3 sheet pans in the oven, whole '
        'kitchen smelling like seasoned chicken and paprika.',
        'meal prep sheet pan oven chicken seasoning batch cook sunday routine'),
    (   'Bulk cooking jasmine rice: 6 cups dry rice in the cooker, storing in '
        'glass Pyrex with a damp paper towel.',
        'bulk rice pyrex storage reheating fluffy jasmine meal prep routine'),
    (   'Canned tuna convenience vs mercury: keeping 4 cans in the pantry for '
        'lazy protein emergencies, spicy mustard trick.',
        'canned tuna mercury fast protein mustard pantry emergency meal'),
    (   'Cottage cheese comeback: blending cottage cheese into pasta sauces or '
        'protein dip, macro cheat code.',
        'cottage cheese blend pasta sauce protein dip macro volume food'),
    (   'Caffeine tolerance resets: surviving 4 days with zero caffeine, '
        'headache withdrawal, first cup back hitting like a truck.',
        'caffeine reset tolerance withdrawal headache coffee first cup energy'),
    (   'Pre-workout beta-alanine tingles: face and fingertips feeling like '
        'ants are crawling under skin, love it vs hate it.',
        'beta alanine tingles face itch pre workout paresthesia energy'),
    (   'High stim pre-workouts: 400mg caffeine plus exotic stims, crashing at '
        '2pm, switching to pump formulas.',
        'high stim preworkout crash 400mg caffeine pump nonstim formula'),
    (   'Non-stim pump formulas: L-Citrulline 8g-10g, Nitrosigine, Glycerol, '
        'roadmap vascularity without the jitters.',
        'non stim pump l citrulline nitrosigine glycerol vascularity no '
        'jitters'),
    (   'Electrolyte salt packets: LMNT vs DIY salt/potassium mix, saving $40 '
        'a month making your own.',
        'lmnt electrolytes diy salt potassium bulk mix savings hydration'),
    (   'Gym towel hygiene: using the same gym towel for 3 days vs fresh towel '
        'every workout, stopping face acne.',
        'gym towel fresh wash hygiene face acne skin bacteria wipe'),
    (   'Lockers with combination locks: forgetting which locker you put your '
        'gym bag in after a brutal session.',
        'locker gym combination lock forgot number tired brain post workout'),
    (   'Front desk staff at the gym: the friendly greeting vs scanning badge '
        'and putting headphones in immediately.',
        'front desk gym greeting scan headphones social energy lift'),
    (   'Peak gym hours 5:30pm chaos: every bench taken, circling the floor '
        'like a vulture waiting for cables to open.',
        'peak hours 5pm gym busy wait benches cables crowd patience'),
    (   'Late night 10pm gym serenity: only 4 people in the whole facility, '
        'playing your own music, pure focus.',
        'late night gym 10pm empty peaceful music focus supersets'),
    (   'Early morning 5am lifter discipline: freezing car, pitch black '
        'outside, having the whole day won before 7am.',
        '5am gym morning early cold dark discipline win day mental edge'),
    (   'Weekend morning gym session: relaxed pace, no work emails pending, '
        'taking 3-minute rests between heavy sets.',
        'weekend morning workout relaxed rest sets no rush coffee lift'),
    (   'Gym gear upgrades: Pioneer leather belt vs cheap Amazon velcro belt, '
        'night and day core bracing stability.',
        'pioneer belt leather lever prong velcro upgrade core brace'),
    (   'Lifting chalk on black gym clothes: white chalk handprints covering '
        'black shirts and shorts, badge of honor.',
        'chalk marks black clothes shorts gym mess lifting badge honor'),
    (   'Calluses on palms: pumice stone vs shaving dead skin with a razor, '
        'stopping calluses from ripping off on deadlifts.',
        'calluses palms rip deadlift razor pumice care grip skin'),
    (   'Thumb tear on hook grip: wrapping thumbs with athletic tape, '
        'surviving heavy pulls without tearing skin.',
        'hook grip thumb tape athletic deadlift tear skin pain traction'),
    (   'Gym water fountain pressure: fountains that only trickle water vs '
        'modern bottle refill stations that fill in 5s.',
        'water fountain gym pressure bottle refill station cold hydrations'),
    (   'Protein powder flavors that taste like chalk: buying a 5lb tub of '
        'cheap protein and regretting every scoop.',
        'protein powder flavor bad chalk 5lb tub regret finish mix oats'),
    (   'Mixing protein into hot oatmeal: whey curdling into weird chunks if '
        'the oats are too hot, letting it cool first.',
        'protein oatmeal proats curdle hot chunks mix cool stir trick'),
    (   'Cinnamon on everything: adding cinnamon to oats, sweet potatoes, and '
        'yogurt for glucose control and taste.',
        'cinnamon oats yogurt sweet potato glucose insulin flavor spice'),
    (   'BCAA powders: whether anyone still buys flavored BCAAs when EAA or '
        'plain whey protein exists.',
        'bcaa eaa amino acids waste money whey protein hydration drink'),
    (   'Pre-workout face wash: washing gym sweat off face immediately to '
        'prevent clogged pores and sweat breakouts.',
        'face wash post workout sweat pores acne skin care shower gym'),
    (   'Cold shower after morning workout: pure adrenaline shock, waking up '
        'system before entering corporate meetings.',
        'cold shower morning wake adrenaline shock energy focus corporate'),
    (   'Evening stretching before bed: 10 minutes of hamstrings and lower '
        'back decompression to ease sleep tension.',
        'stretching evening bed hamstrings back decompression relax sleep'),
    (   'Hydration tracking apps: logging water vs just drinking when thirsty '
        'and checking urine color.',
        'hydration tracking water apps urine color simple rule hydration'),
    (   'Weekend meal prep containers stacking: fridge looking like a military '
        'supply depot, ready for the week.',
        'meal prep fridge containers stacked military clean organized week '
        'ready'),
    (   'Lifting partner form checks: sending 5-second video clips of squat '
        'depth or bench bar path on WhatsApp.',
        'form check video squat bench bar path depth feedback partner lift'),
    (   'Ego lifting reality checks: dropping the weight 20% to actually feel '
        'the target muscle stretch and contract.',
        'ego lift drop weight target muscle mind muscle stretch contract '
        'growth'),
    (   'Mind-muscle connection: pausing at peak contraction on cable rows vs '
        'just yanking the stack with momentum.',
        'mind muscle connection pause contraction cable row momentum form'),
    (   'Tempo training: 3-second eccentric descent on bench press, building '
        'tendon strength and hypertrophy.',
        'tempo eccentric 3 seconds descent bench control hypertrophy tendon'),
    (   'Rest-pause sets: hitting failure, resting 15 seconds, squeezing out 3 '
        'more reps, ultimate intensity technique.',
        'rest pause sets failure 15 sec reps intensity overload hypertrophy'),
    (   'Drop sets on lateral raises: running the dumbbell rack down from 35s '
        'to 15s until delts feel like lava.',
        'drop set lateral raise run rack delts burn lava intensity volume'),
    (   'Myofascial trigger points: using a lacrosse ball on glutes and upper '
        'back, painful knots releasing.',
        'lacrosse ball trigger point glutes upper back knots release mobility '
        'pain')]

SESSION_3_THEMES = [   (   'Nootropic focus stacks: Semax vs Selank nasal drops, laser focus vs '
        'calming social anxiety, work productivity.',
        'semax selank nasal spray nootropic focus anxiety work'),
    (   'Bromantane dopamine upregulation: conquering afternoon workplace '
        'brain fog, physical stamina without jittery crash.',
        'bromantane dopamine focus stamina afternoon fog energy nootropic'),
    (   'Modafinil / Armodafinil for brutal work sprints: staying locked into '
        'spreadsheets for 10 hours, hydration discipline.',
        'modafinil armodafinil focus work sprint spreadsheet tunnel vision'),
    (   'Phenylpiracetam for cold weather workouts: intense physical drive, '
        'sharp mental clarity, tolerance cycling rules.',
        'phenylpiracetam piracetam racetam cold focus drive tolerance cycle'),
    (   'Alpha-GPC & Choline sources: fueling acetylcholine for heavy lifting '
        'focus, avoiding choline depression by dialing dose.',
        'alpha gpc choline acetylcholine focus lifting depression dose'),
    (   'L-Tyrosine + Caffeine synergy: taking 1.5g L-Tyrosine with morning '
        'espresso, acute dopamine precursor for deep work.',
        'tyrosine caffeine espresso dopamine deep work morning focus stack'),
    (   'L-Theanine smoothing out caffeine: 2:1 theanine to caffeine ratio, '
        'stopping heart jitters and keeping clean focus.',
        'theanine caffeine ratio jitters smooth calm focus desk energy'),
    (   'Magnesium L-Threonate for brain & sleep: crossing blood-brain '
        'barrier, vivid dreams, waking up with clear head.',
        'magnesium threonate brain blood barrier sleep dreams clarity'),
    (   'Lion’s Mane mushroom extract: NGF stimulation, memory recall, '
        'checking for beta-glucan percentages on labels.',
        'lions mane mushroom ngf memory recall beta glucan quality'),
    (   'Noopept fast-acting sublingual: acute 2-hour focus window for '
        'presentations and exams, tiny 10mg-20mg scoops.',
        'noopept sublingual scoop acute focus presentation exam dose fast'),
    (   'Conquering the 2pm corporate afternoon crash: ditching heavy carb '
        'lunches for fats/protein, light walking outside.',
        'afternoon crash 2pm lunch carbs fats protein walk sunlight energy'),
    (   'Dopamine detox weekends: turning off notifications, no social media '
        'scrolling, resetting baseline motivation.',
        'dopamine detox screen time phone scroll baseline reset motivation '
        'weekend'),
    (   'Deep work 90-minute blocks: phone in another room, noise-cancelling '
        'headphones, getting 8 hours of work done in 3.',
        'deep work blocks focus phone away headphones productivity sprint'),
    (   'Oura Ring sleep staging accuracy: tracking deep sleep vs REM '
        'percentages, why late night meals crush recovery scores.',
        'oura ring sleep staging deep rem late meal heart rate recovery'),
    (   'Whoop recovery score obsession: waking up in the yellow vs green, '
        'deciding whether to hit heavy deadlifts or deload.',
        'whoop recovery score strain green yellow deadlift workout decision'),
    (   'Sleep latency issues: lying in bed with racing mind, 4-7-8 breathing '
        'technique, turning off phone screen 1 hour before.',
        'sleep latency racing thoughts breathing 478 screen off sleep '
        'insomnia'),
    (   'Ashwagandha KSM-66 vs Sensoril: reducing cortisol, emotional numbness '
        '/ anhedonia after months of continuous use.',
        'ashwagandha ksm66 sensoril cortisol anhedonia emotional blunt cycle'),
    (   'Rhodiola Rosea adaptogen: beating physical and mental burnout, '
        'salidrosides vs rosavins, clean morning energy.',
        'rhodiola rosea adaptogen burnout fatigue energy salidroside morning'),
    (   'UFC fight night main event breakdown: analyzing striking matchups, '
        'takedown defense, championship round pacing.',
        'ufc fight night main event breakdown striking takedown cardio pace'),
    (   'UFC championship title fight debates: goat status discussions, '
        'judging controversies, 10-8 round criteria.',
        'ufc title goat debates judging robbery 108 round decision scoring'),
    (   'BJJ open mat culture: survive vs submit, the 50-year-old black belt '
        'destroying 25-year-old bodybuilders with technique.',
        'bjj open mat survive submit black belt technique leverage rolls'),
    (   'Cauliflower ear prevention: draining fluid with insulin pins, wearing '
        'headgear, the badge of grappling honor.',
        'cauliflower ear drain needle headgear grappling bjj wrestling ear'),
    (   'Rib subluxation & intercostal tears from grappling: having a 220lb '
        'brown belt knee-on-belly you, recovery timelines.',
        'rib injury subluxation knee on belly bjj grappling healing bpc '
        'recovery'),
    (   'Finger tape and grip arthritis in BJJ: spider guard grip wear and '
        'tear, buddy taping fingers before rolling.',
        'finger tape bjj grip arthritis spider guard gi joints hand care'),
    (   'Weight cuts in combat sports: water loading, hot salt baths, sauna '
        'suits, the mental warfare of hitting weight.',
        'weight cut combat mma sauna salt bath water load weigh in sweat'),
    (   'Boxing sparring etiquette: learning to pull punches at 50% vs gym '
        'wars with guys trying to knock heads off.',
        'boxing sparring etiquette 50 percent gym wars control head trauma '
        'safety'),
    (   'Neck training for concussion prevention: harness extensions, neck '
        'curls on bench, protecting cervical spine in combat.',
        'neck training harness curls concussion protection cervical spine '
        'combat'),
    (   'Grip strength training: captain of crush grippers, farmer carries, '
        'towel pullups, transfer to grappling control.',
        'grip strength captain crush farmer carry towel pullup grappling hold'),
    (   'Zone 2 cardio for combat gas tanks: 45 minutes on Airdyne bike at '
        'nose-breathing pace, building aerobic base.',
        'zone2 cardio airdyne bike aerobic base gas tank combat rounds '
        'stamina'),
    (   'HIIT sprints on assault bike: 10 seconds all-out, 20 seconds rest, '
        'tasting copper in your mouth after 8 rounds.',
        'assault bike hiit sprints tabata copper taste lung burn anaerobic'),
    (   'Khabib style ground control vs modern guard play: top pressure, leg '
        'triangles, breaking opponents willpower.',
        'khabib dagestan top pressure ground control bjj wrestling wrist ride'),
    (   'MMA striking transitions: level changes, fake jabs into double leg '
        'takedowns, distance management.',
        'mma striking level change takedown double leg distance timing cage'),
    (   'Knee injuries in grappling: meniscus twists, heel hook defense, '
        'knowing when to tap immediately without ego.',
        'knee injury heel hook meniscus tap early bjj submission defense '
        'safety'),
    (   'Shin conditioning in Muay Thai: heavy bag kicking, conditioning shin '
        'bones through microfractures over years.',
        'muay thai shin conditioning heavy bag kicking bone density kicks'),
    (   'Pre-fight anxiety and adrenaline dumps: legs feeling heavy walking to '
        'the cage, breathing to calm heart rate.',
        'fight anxiety adrenaline dump cage walk breathing tunnel vision calm'),
    (   'Ice bath post-combat session: numbing battered shins and bruised '
        'ribs, reducing whole-body inflammation.',
        'ice bath combat bruised shins ribs inflammation recovery cold '
        'session'),
    (   'Turmeric & Curcumin with Piperine: natural joint anti-inflammatory, '
        'taking 1000mg with healthy fats.',
        'curcumin turmeric piperine black pepper inflammation joints natural'),
    (   'Boswellia Serrata for joint mobility: 5-LOX inhibition, soothing '
        'inflamed knees and elbows naturally.',
        'boswellia joint mobility 5lox inflammation knees elbows natural herb'),
    (   'Apigenin chamomile extract: GABA receptor calming before bed, '
        'drifting off without morning grogginess.',
        'apigenin chamomile gaba sleep natural relaxation night unwind'),
    (   'GABA + L-Arginine for GH release: taking on empty stomach before bed, '
        'warm tingling flush, deep sleep enhancement.',
        'gaba arginine sleep gh release flush tingles deep sleep night'),
    (   'Melatonin microdosing: why 300mcg works better than 10mg megadoses '
        'without daytime grogginess or nightmare spikes.',
        'melatonin microdose 300mcg dreams sleep groggy dose timing'),
    (   'Tart cherry juice for sleep: natural melatonin and anthocyanins, '
        'drinking 4oz with sparkling water post-dinner.',
        'tart cherry juice melatonin sleep recovery soreness anthocyanin '
        'night'),
    (   'Late-night shower thought: why does every gym playlist sound like it '
        'was made in 2013 by an angry teenager.',
        'shower thought gym playlist angry teenager 2013 edm music funny'),
    (   'Late-night thought: how crazy is it that humans lift heavy metal bars '
        'off the floor just to put them right back down.',
        'shower thought deadlift heavy metal floor put down existential '
        'lifting'),
    (   'Late-night thought: imagine explaining to someone in 1500 that we run '
        'on treadmills going nowhere inside air-conditioned rooms.',
        'shower thought treadmill running nowhere air conditioning modern '
        'life'),
    (   'Late-night thought: protein synthesis is literally your body '
        'rebuilding torn muscle fibers with chicken atoms.',
        'shower thought muscle rebuild chicken atoms hypertrophy biology food'),
    (   'Late-night thought: the weird bond between two guys who nod at each '
        'other in the gym at 6am for 3 years without speaking.',
        'shower thought gym nod 6am regulars silent brotherhood bond'),
    (   'Late-night thought: pre-workout caffeine doses would give an '
        '18th-century peasant a spiritual awakening.',
        'shower thought peasant preworkout caffeine heart attack Victorian '
        'times'),
    (   'Late-night thought: our ancestors walked 20 miles hunting woolly '
        'mammoths and we get tired walking up 3 flights of stairs.',
        'shower thought ancestors mammoth hunt steps stairs cardio laziness'),
    (   'Late-night thought: the psychological relief when you finally sit '
        'down in the car after an absolute monster workout.',
        'shower thought car seat sit post workout relief exhaustion peace '
        'drive'),
    (   'Late-night thought: the strange comfort of drinking cold water from a '
        'shaker cup at 3am.',
        'shower thought cold water 3am night shaker cup thirst bedroom'),
    (   'Late-night thought: why does gym lighting look 10x better in '
        'commercial gyms than in your own bathroom.',
        'shower thought downlight gym lighting mirror bathroom body '
        'dysmorphia'),
    (   'Late-night thought: your body is an engine that turns steak, rice, '
        'and water into 400lb barbell squats.',
        'shower thought engine steak rice barbell squat physics human machine'),
    (   'Creatine monohydrate cognitive boost: brain bioenergetics, memory '
        'retention in sleep-deprived conditions.',
        'creatine brain cognitive focus memory sleep deprivation energy atp'),
    (   'Huperzine A acetylcholine preservation: inhibiting '
        'acetylcholinesterase, acute memory focus, cycling 2 days on 1 off.',
        'huperzine memory acetylcholine focus cycle nootropic brain sharp'),
    (   'Choline bitartrate vs Citicoline (CDP-Choline): why CDP-Choline '
        'provides uridine for dopamine receptors and focus.',
        'cdp choline citicoline uridine dopamine receptors nootropic memory'),
    (   'Sulbutiamine synthetic B1 derivative: conquering chronic fatigue and '
        'social anxiety, dopaminergic boost.',
        'sulbutiamine vitamin b1 fatigue social energy focus dopamine drive'),
    (   'Fasoracetam GABA-B upregulation: resetting tolerance to Phenibut or '
        'prescription anxiolytics, mental clarity.',
        'fasoracetam gaba tolerance reset anxiolytic mental focus calm'),
    (   'Aniracetam mood elevation & creativity: AMPA receptor modulation, '
        'social fluidity in meetings, fat-soluble with meal.',
        'aniracetam racetam mood creativity social meeting ampa fat soluble'),
    (   'Oxiracetam logical reasoning drive: clean cognitive stimulant, '
        'tackling heavy math, coding, and technical workflows.',
        'oxiracetam logic math coding technical focus clean stimulant racetam'),
    (   'Centrophenoxine DMAE derivative: clearing cellular lipofuscin brain '
        'waste, mental speed, membrane fluidity.',
        'centrophenoxine dmae lipofuscin brain clean memory speed aging'),
    (   'Uridine monophosphate + DHA stack: synaptogenesis, neuroplasticity, '
        'rebuilding dopamine receptor sensitivity.',
        'uridine dha fish oil synaptogenesis neuroplasticity dopamine '
        'receptors'),
    (   'N-Acetyl L-Cysteine (NAC) for OCD and intrusive thoughts: glutamate '
        'regulation in the brain, mental calm.',
        'nac glutamate intrusive thoughts mental calm brain obsessive anxiety'),
    (   'Inositol for night anxiety: 2g-5g inositol powder in water, calming '
        'central nervous system, stopping panic wakeups.',
        'inositol sleep anxiety panic nervous system powder night calm'),
    (   'Glycine 3g before bed: lowering core body temperature, improving '
        'subjective sleep quality and morning alertness.',
        'glycine sleep core temperature body rem recovery alertness morning'),
    (   'L-Tryptophan vs 5-HTP: serotonin precursors, why tryptophan provides '
        'steadier sleep support without serotonin syndrome.',
        'tryptophan 5htp serotonin sleep mood synthesis natural night'),
    (   'Theobromine cocoa extract: smoother and longer-lasting than caffeine, '
        'gentle bronchodilator for cardio training.',
        'theobromine cocoa caffeine smooth energy cardio bronchodilator mild'),
    (   'Dynamine (Methylliberine) clean energy: fast onset, zero crash, '
        'pairing with caffeine for high-paced work mornings.',
        'dynamine methylliberine caffeine stack energy fast focus no crash'),
    (   'Teacrine (Theacrine) non-habit-forming energy: 6-hour smooth energy '
        'curve, no tolerance buildup, pre-workout boost.',
        'teacrine theacrine tolerance clean energy caffeine booster endurance'),
    (   'Cordyceps mushroom for ATP output: increasing VO2 max, oxygen '
        'utilization during intense combat rounds or intervals.',
        'cordyceps mushroom vo2 max oxygen atp endurance cardio stamina'),
    (   'Reishi mushroom for evening winding down: immune modulating '
        'triterpenes, calming nervous system before sleep.',
        'reishi mushroom evening unwind nervous system calm immune sleep'),
    (   'Chaga antioxidant powerhouse: cellular health, brewing wild chaga tea '
        'with raw honey and cinnamon.',
        'chaga tea antioxidant cellular immunity wild mushroom morning'),
    (   'Shilajit fulvic acid trace minerals: increasing natural testosterone, '
        'cellular energy, testing for heavy metal purity.',
        'shilajit fulvic acid trace minerals energy test purity lab test'),
    (   'Pine pollen phytoandrogens: natural DHEA and androgens, sublingual '
        'alcohol tinctures, energy boost.',
        'pine pollen phytoandrogens dhea tincture energy natural hormonal'),
    (   'Tongkat Ali (Eurycoma Longifolia): lowering SHBG, free testosterone '
        'boost, cycling 5 days on 2 days off to prevent tolerance.',
        'tongkat ali eurycoma shbg free test herbal libido energy cycle'),
    (   'Fadogia Agrestis LH mimicry: stimulating Leydig cells, cycling rules '
        'to protect testicular and kidney tissue.',
        'fadogia agrestis lh leydig testicle herbal test booster cycle safe'),
    (   'Boron 10mg cycling for free test: taking 10mg daily for 2 weeks to '
        'drop SHBG, pausing 1 week to avoid estrogen rebound.',
        'boron shbg free test cycle estrogen balance minerals'),
    (   'Zinc Picolinate + Copper balance: 30mg zinc with 2mg copper, '
        'preventing zinc-induced copper deficiency and anemia.',
        'zinc picolinate copper balance immunity testosterone minerals ratio'),
    (   'Vitamin D3 + K2 (MK-7) mega-optimization: 5000-10000 IU daily with '
        'fatty meal, calcium absorption, immune health.',
        'vitamin d3 k2 mk7 calcium bone immunity sunshine energy blood levels'),
    (   'Vitamin B12 methylcobalamin: sublingual lozenges for nerve health, '
        'red blood cell production, beating fatigue.',
        'vitamin b12 methylcobalamin nerve rbc energy sublingual fatigue'),
    (   'CoQ10 Ubiquinol vs Ubiquinone: mitochondrial ATP production, heart '
        'muscle support, reversing statin muscle fatigue.',
        'coq10 ubiquinol ubiquinone heart atp mitochondria cardio stamina'),
    (   'PQQ (Pyrroloquinoline Quinone): stimulating mitochondrial biogenesis, '
        'creating new cellular power plants with CoQ10.',
        'pqq mitochondria biogenesis cellular energy coq10 brain health'),
    (   'Resveratrol + NMN longevity stack: NAD+ boosting, sirtuin activation, '
        'morning energy and DNA repair protocols.',
        'nmn resveratrol nad sirtuins longevity dna repair cellular energy'),
    (   'Metformin vs Berberine for glucose disposal: 500mg berberine before '
        'carb meals, AMPK activation, fat loss support.',
        'berberine metformin glucose disposal ampk carbs insulin sensitivity'),
    (   'Apple cider vinegar with mother before heavy meals: blunting '
        'postprandial glucose spikes, aiding stomach acid.',
        'apple cider vinegar glucose spike digestion stomach acid prebiotic'),
    (   'Digestive enzymes with ox bile: breaking down 300g protein and 100g '
        'fats without bloating or toilet emergencies.',
        'digestive enzymes ox bile protein fat breakdown bloat digestion '
        'relief'),
    (   'Betaine HCl for low stomach acid: fixing acid reflux by increasing '
        'stomach acid, ensuring complete protein breakdown.',
        'betaine hcl stomach acid acid reflux digestion protein absorption'),
    (   'Probiotics & Prebiotics: fermented kimchi, kefir, building diverse '
        'gut microbiome for neurotransmitter synthesis.',
        'probiotics kefir kimchi gut microbiome serotonin digestion health'),
    (   'Bone broth collagen for gut lining: drinking warm bone broth with '
        'ginger and turmeric on empty stomach.',
        'bone broth collagen gut lining ginger turmeric joint stomach soothe'),
    (   'Glutamine 10g-15g daily: healing intestinal permeability (leaky gut), '
        'muscle glycogen recovery, zero taste.',
        'glutamine leaky gut intestines recovery immune powder carbs clean'),
    (   'BJJ Gi vs No-Gi conditioning: why No-Gi is lightning fast pace while '
        'Gi is grueling isometric grip battle.',
        'bjj gi nogi pace grip isometric cardio speed technique wrestling'),
    (   'Wrestling single leg vs double leg defense: sprawls, underhooks, '
        'whizzers, heavy hips stopping takedowns.',
        'wrestling sprawl underhook takedown defense hips combat mma'),
    (   'MMA striking footwork: cutting angles, ring generalship, avoiding '
        'straight-line retreats into the cage.',
        'footwork angles cage ring generalship movement head combat boxing'),
    (   'Shin guards selection: Hayabusa vs Fairtex for heavy sparring, instep '
        'protection, stopping bone bruises.',
        'shin guards hayabusa fairtex muay thai sparring protection padding'),
    (   'Mouthguard custom dental molds vs boil-and-bite: breathing ease '
        'during round 5, teeth protection.',
        'mouthguard dental mold custom boil bite breathing teeth combat '
        'protection'),
    (   'Boxing hand wrap technique: 180-inch wraps, protecting knuckles, '
        'locking wrist bones into solid cast.',
        'hand wraps boxing 180 inch knuckles wrist cast punch protection'),
    (   'Speed bag training: shoulder endurance, rhythmic timing, keeping '
        'hands up when exhausted in late rounds.',
        'speed bag rhythm shoulder endurance timing hands up boxing cardio'),
    (   'Heavy bag power punching: hip torque, breathing out on impact, '
        'turning hips over on rear hook.',
        'heavy bag power punch hip torque breath impact hooks boxing'),
    (   'Muay Thai clinch knee strikes: head control, plum grip, turning '
        'opponent into heavy knee strikes.',
        'muay thai clinch plum knee strikes head control leverage combat'),
    (   'Sambo leg locks vs modern American BJJ: saddle position, inside heel '
        'hooks, leg entanglement race.',
        'sambo leg locks bjj saddle heel hook entanglement submission fast'),
    (   'Judo throws in No-Gi: harai goshi, uchi mata, using opponent forward '
        'momentum to launch them.',
        'judo throws uchi mata harai goshi nogi momentum takedown combat'),
    (   'End of workout endorphin rush: lying on the gym mat staring at the '
        'ceiling, feeling completely at peace with life.',
        'endorphins post workout mat ceiling peace calm life reset heavy lift')]

DEFAULT_DAILY_THEMES = SESSION_1_THEMES + SESSION_2_THEMES + SESSION_3_THEMES


BANNED_DIALOGUE_PATTERNS = [
    re.compile(r"\bliver\s*king\b", re.I),
    re.compile(r"\braw\s*liver\b", re.I),
    re.compile(r"\bancestral\s*tenets?\b", re.I),
    re.compile(r"\bancestral\s*lifestyle\b", re.I),
    re.compile(r"\bduck\s*eggs?\b", re.I),
    re.compile(r"\bbaby\s*don't\s*hurt\s*me\b", re.I),
]


def sanitize_banned_tropes(text):
    """Actively neutralizes stale meme tropes and banned dialogue patterns."""
    if not text:
        return text
    for pat in BANNED_DIALOGUE_PATTERNS:
        if pat.search(text):
            text = pat.sub("some fitness influencer", text)
    return text

class DailyBatchEngine:
    def is_candidate_awake(self, cand, ts=None):
        """Checks if a persona is awake at a given timestamp according to their individual sleep window."""
        if ts is None:
            ts = time.time()
        from datetime import datetime as _dt
        local_dt = _dt.fromtimestamp(ts)
        local_hour = local_dt.hour + (local_dt.minute / 60.0)

        p_seed_str = str(cand.get("persona_id") or cand.get("id") or cand.get("persona_name") or "default")
        seed_hash = sum(ord(c) for c in p_seed_str)
        archetype_idx = seed_hash % 6
        SCHEDULES = [
            (23.0, 6.5),  # early_bird
            (23.5, 7.0),  # standard
            (0.5, 7.5),   # moderate_night_owl
            (1.0, 7.5),   # night_owl
            (1.5, 8.0),   # flexible_sleeper
            (22.5, 6.0),  # morning_athlete
        ]
        s_start, s_end = SCHEDULES[archetype_idx]
        tz_offset = float(((seed_hash % 7) - 3) * 1.0)
        tz_hour = (local_hour + tz_offset) % 24.0
        if s_start <= s_end:
            asleep = (s_start <= tz_hour < s_end)
        else:
            asleep = (tz_hour >= s_start or tz_hour < s_end)
        return not asleep

    def __init__(self, store, ai_module, memory_engine=None):
        self.store = store
        self.ai = ai_module
        self.memory = memory_engine

    def today_str(self):
        return datetime.now().strftime("%Y-%m-%d")

    def get_group_candidates(self, chat_id):
        """Candidate resolution strictly limited to accounts and personas explicitly assigned or belonging to this group."""
        chat_id = int(chat_id)
        candidates = []
        seen = set()

        # 1. group_personas with explicit account_id
        gp_rows = self.store.rows(
            "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, gp.account_id, a.name as account_name, a.phone as account_phone, a.username as account_username "
            "FROM group_personas gp "
            "JOIN personas p ON p.id=gp.persona_id "
            "JOIN accounts a ON a.id=gp.account_id "
            "WHERE gp.chat_id=? AND a.active=1",
            (chat_id,)
        )
        for r in gp_rows:
            key = (r["persona_id"], r["account_id"])
            if key not in seen:
                candidates.append(r)
                seen.add(key)

        # 2. group_accounts where account has persona_id assigned
        ga_rows = self.store.rows(
            "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, a.id as account_id, a.name as account_name, a.phone as account_phone, a.username as account_username "
            "FROM group_accounts ga "
            "JOIN accounts a ON a.id=ga.account_id "
            "JOIN personas p ON p.id=a.persona_id "
            "WHERE ga.chat_id=? AND a.active=1",
            (chat_id,)
        )
        for r in ga_rows:
            key = (r["persona_id"], r["account_id"])
            if key not in seen:
                candidates.append(r)
                seen.add(key)

        # 3. Group owner account's persona if explicitly linked to this group
        g_rows = self.store.rows("SELECT account_id FROM groups WHERE chat_id=?", (chat_id,))
        if g_rows and g_rows[0].get("account_id"):
            g_aid = g_rows[0]["account_id"]
            owner_rows = self.store.rows(
                "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, a.id as account_id, a.name as account_name, a.phone as account_phone, a.username as account_username "
                "FROM accounts a "
                "JOIN personas p ON p.id=a.persona_id "
                "WHERE a.id=? AND a.active=1",
                (g_aid,)
            )
            for r in owner_rows:
                key = (r["persona_id"], r["account_id"])
                if key not in seen:
                    candidates.append(r)
                    seen.add(key)

        # 4. Broad Fallback for Watched Groups: Ensure all active accounts with personas participate
        g_watch = self.store.rows("SELECT watched FROM groups WHERE chat_id=?", (chat_id,))
        is_watched = bool(g_watch and g_watch[0].get("watched"))
        if is_watched or len(candidates) < 2:
            all_active = self.store.rows(
                "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, a.id as account_id, a.name as account_name, a.phone as account_phone, a.username as account_username "
                "FROM accounts a "
                "JOIN personas p ON p.id=a.persona_id "
                "WHERE a.active=1 AND (a.is_admin=0 OR a.is_admin IS NULL) AND LOWER(COALESCE(a.username,'')) NOT LIKE '%xbiolabs%'"
            )
            for r in all_active:
                key = (r["persona_id"], r["account_id"])
                if key not in seen:
                    candidates.append(r)
                    seen.add(key)

        # 5. Guaranteed Participation Fallback: Pair active accounts with available personas
        if len(candidates) < 2:
            all_personas = self.store.rows("SELECT id as persona_id, name as persona_name, details, prompt FROM personas")
            all_active_accs = self.store.rows("SELECT id as account_id, name as account_name, phone as account_phone, username as account_username FROM accounts WHERE active=1 AND (is_admin=0 OR is_admin IS NULL) AND LOWER(COALESCE(username,'')) NOT LIKE '%xbiolabs%'")
            for i, acc in enumerate(all_active_accs):
                if all_personas:
                    p = all_personas[i % len(all_personas)]
                    cand = {**p, **acc}
                    key = (cand["persona_id"], cand["account_id"])
                    if key not in seen:
                        candidates.append(cand)
                        seen.add(key)

        return candidates

    async def generate_all_groups(self, count=None):
        """Generates today's batch schedule for all eligible groups in one shot."""
        if count is None:
            count = int(self.store.get("batch_messages_count", 24))
        today = self.today_str()
        groups = self.store.rows("SELECT * FROM groups WHERE watched=1")
        all_cids = [g["chat_id"] for g in groups]

        results = []
        for cid in all_cids:
            cands = self.get_group_candidates(cid)
            if len(cands) >= 2:
                try:
                    res = await self.generate_batch_for_group(cid, count=count)
                    g_match = [g for g in groups if g["chat_id"] == cid]
                    title = g_match[0].get("title") if g_match else f"Group {cid}"
                    results.append({"chat_id": cid, "title": title, "count": res.get("count", 0)})
                except Exception as e:
                    log.warning("Failed to generate batch for group %s: %s", cid, e)
        return results

    async def generate_batch_for_group(self, chat_id, count=48, topic_override=None):
        """Generates today's batch schedule for a watched group."""
        chat_id = int(chat_id)
        today = self.today_str()

        # Check for active existing batch for today
        if not topic_override:
            existing = self.store.rows("SELECT id, topic FROM daily_batches WHERE chat_id=? AND date_str=?", (chat_id, today))
            if existing:
                pending_check = self.store.rows(
                    "SELECT COUNT(*) as cnt FROM daily_batch_items WHERE chat_id=? AND status IN ('pending', 'postponed') AND scheduled_ts > ?",
                    (chat_id, int(time.time()))
                )
                cnt = (pending_check[0]["cnt"] if pending_check else 0)
                if cnt > 2:
                    log.info("generate_batch_for_group: Batch for group %s on %s has %d upcoming items (id=%s). Holding next wave.",
                             chat_id, today, cnt, existing[0]["id"])
                    return {"count": 0, "status": "already_exists", "batch_id": existing[0]["id"]}

        # Hard budget cap guard ($0.05/day for automated batch dialogue)
        can_batch, spent, cap = self.store.check_batch_budget_available()
        if not can_batch:
            log.warning(
                "[BUDGET CAP] Daily batch spending cap ($%.2f) reached ($%.4f spent today). "
                "Halting batch generation for group %s until midnight reset.",
                cap, spent, chat_id
            )
            return {"status": "budget_capped", "spent": spent, "cap": cap, "count": 0}

        g_rows = self.store.rows("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
        if not g_rows:
            raise ValueError(f"Group {chat_id} not found")
        group = g_rows[0]

        # 1. Fetch assigned personas with accounts using unified candidate resolver
        candidates = self.get_group_candidates(chat_id)

        if not candidates:
            raise ValueError("No active accounts with personas found for this group. Link personas to accounts first.")

        # 2. Get recent chat context and multi-day condensed conversation history
        recent_msgs = self.store.recent(chat_id, limit=12)
        transcript_lines = []
        for m in recent_msgs:
            if m.get("text"):
                transcript_lines.append(f"[{m.get('sender', 'Member')}]: {m.get('text')}")
        recent_transcript = "\n".join(transcript_lines) or "(Group chat is fresh / quiet)"

        condensed_history = ""
        if hasattr(self.store, "get_condensed_conversation_history"):
            condensed_history = self.store.get_condensed_conversation_history(chat_id, max_msgs=16, lookback_hours=72)
        if not condensed_history or condensed_history.startswith("("):
            condensed_history = recent_transcript

        # 2B. Build ABSOLUTE ZERO REPETITION blacklist from the last 48 hours
        now_ts = int(time.time())
        past_batch_msgs = self.store.rows(
            "SELECT text FROM daily_batch_items WHERE chat_id=? AND (created_at > ? OR scheduled_ts > ?) "
            "ORDER BY id DESC LIMIT 50",
            (chat_id, now_ts - 172800, now_ts - 172800)
        )
        forbidden_snippets = []
        for pbm in past_batch_msgs:
            t = (pbm.get("text") or "").strip()
            if t and len(t) > 12 and not t.startswith("[reaction:"):
                forbidden_snippets.append(t[:75].replace("\n", " "))
        for rm in recent_msgs:
            t = (rm.get("text") or "").strip()
            if t and len(t) > 12:
                forbidden_snippets.append(t[:75].replace("\n", " "))

        forbidden_snippets = list(dict.fromkeys(forbidden_snippets))[:30]
        forbidden_block = "\n".join(f"  * {s}" for s in forbidden_snippets) if forbidden_snippets else "  * (None yet - keep discussion completely fresh)"

        # 2C. Determine topic context with strict rotation across unused themes
        recent_batches = self.store.rows(
            "SELECT topic FROM daily_batches WHERE chat_id=? ORDER BY id DESC LIMIT 15",
            (chat_id,)
        )
        used_past_topics = [b.get("topic") or "" for b in recent_batches]

        # Load persistent theme history from SQLite to prevent repeating themes from the past 14 days
        used_theme_keys = set()
        if hasattr(self.store, "get_recent_used_theme_keys"):
            used_theme_keys = self.store.get_recent_used_theme_keys(chat_id, lookback_days=14)

        default_themes = DEFAULT_DAILY_THEMES
        fresh_themes = [
            (t, q) for (t, q) in default_themes
            if not any(q in ut.lower() or t[:25].lower() in ut.lower() for ut in used_past_topics)
            and q.strip().lower() not in used_theme_keys and t[:30].strip().lower() not in used_theme_keys
        ]
        if not fresh_themes:
            fresh_themes = default_themes

        # 2C. Determine micro-threads plan: split batches into 3 focused micro-threads (Session 1, Session 2, Session 3)
        if topic_override or count < 15:
            micro_plans = [(topic_override, count)]
        elif count >= 21:
            base = count // 3
            rem = count - (base * 3)
            micro_plans = [(None, base + (1 if i < rem else 0)) for i in range(3)]
        else:
            base = count // 2
            rem = count - (base * 2)
            micro_plans = [(None, base + (1 if i < rem else 0)) for i in range(2)]

        domain_compounds = [t for t in fresh_themes if t in SESSION_1_THEMES] or SESSION_1_THEMES
        domain_banter = [t for t in fresh_themes if t in SESSION_2_THEMES] or SESSION_2_THEMES
        domain_culture = [t for t in fresh_themes if t in SESSION_3_THEMES] or SESSION_3_THEMES

        thread_configs = []
        chosen_tuples = []
        for p_i, (t_override, p_count) in enumerate(micro_plans):
            if t_override:
                thread_configs.append((t_override, t_override, p_count))
            elif len(micro_plans) == 3:
                if p_i == 0:
                    avail = [t for t in domain_compounds if t not in chosen_tuples] or SESSION_1_THEMES
                elif p_i == 1:
                    avail = [t for t in domain_banter if t not in chosen_tuples] or SESSION_2_THEMES
                else:
                    avail = [t for t in domain_culture if t not in chosen_tuples] or SESSION_3_THEMES
                chosen_t = random.choice(avail)
                chosen_tuples.append(chosen_t)
                thread_configs.append((chosen_t[0], chosen_t[1], p_count))
                if hasattr(self.store, "record_used_theme"):
                    self.store.record_used_theme(chat_id, chosen_t[1], chosen_t[0], session_idx=p_i+1)
            elif len(micro_plans) == 2:
                if p_i == 0:
                    avail = [t for t in domain_compounds if t not in chosen_tuples] or SESSION_1_THEMES
                else:
                    avail = [t for t in (domain_banter + domain_culture) if t not in chosen_tuples] or fresh_themes
                chosen_t = random.choice(avail)
                chosen_tuples.append(chosen_t)
                thread_configs.append((chosen_t[0], chosen_t[1], p_count))
                if hasattr(self.store, "record_used_theme"):
                    self.store.record_used_theme(chat_id, chosen_t[1], chosen_t[0], session_idx=p_i+1)
            else:
                chosen_t = random.choice(fresh_themes)
                chosen_tuples.append(chosen_t)
                thread_configs.append((chosen_t[0], chosen_t[1], p_count))
                if hasattr(self.store, "record_used_theme"):
                    self.store.record_used_theme(chat_id, chosen_t[1], chosen_t[0], session_idx=p_i+1)
        # 3. Auto-clear past failed and cancelled items for this chat to keep queue fresh
        try:
            self.store.q("DELETE FROM daily_batch_items WHERE chat_id=? AND status IN ('failed', 'cancelled')", (chat_id,))
            self.store.q("DELETE FROM queue WHERE chat_id=? AND kind='batch_scheduled_send' AND status IN ('failed', 'cancelled')", (chat_id,))
        except Exception as e:
            log.debug("Auto-cleanup failed items: %s", e)

        # 4. Prepare daily_batches row with combined topics summary
        today = self.today_str()
        topic_summary = " | ".join(f"[Session {i+1}] {tc[0][:50]}" for i, tc in enumerate(thread_configs))
        cur_batch = self.store.rows("SELECT id FROM daily_batches WHERE chat_id=? AND date_str=?", (chat_id, today))
        if cur_batch:
            batch_id = cur_batch[0]["id"]
            self.store.q("UPDATE daily_batches SET topic=? WHERE id=?", (topic_summary, batch_id))
        else:
            cur = self.store.q(
                "INSERT INTO daily_batches(date_str, chat_id, topic, created_at) VALUES(?,?,?,?)",
                (today, chat_id, topic_summary, int(time.time()))
            )
            batch_id = cur.lastrowid

        cand_names = [c.get("persona_name") or c.get("account_name") for c in candidates if c.get("persona_name") or c.get("account_name")]
        group_title = group.get("title") or f"Group {chat_id}"
        group_about = group.get("about") or "Telegram community discussion"
        group_domain = group.get("domain_knowledge") or "Lifting, gear, TRT, SARMs, peptides, nootropics, health, celebrity transformations & pop culture, sports & fight nights, breaking news, crypto/tech, work/dating, plus truly random everyday banter."
        settings = {k: self.store.get(k) for k in ("fal_key", "ai_model")}

        sessions_items = []
        session_digests = []
        for th_idx, (th_theme, th_query, th_count) in enumerate(thread_configs):
            can_batch, spent, cap = self.store.check_batch_budget_available()
            if not can_batch:
                log.warning(
                    "[BUDGET CAP] Daily batch spending cap reached mid-generation ($%.4f >= $%.2f). Halting further sessions.",
                    spent, cap
                )
                break
            domain_labels = [
                "Session 1 Domain [Peptides, Compounds, TRT, Bloodwork & Recovery Protocols]",
                "Session 2 Domain [Everyday Life, Hilarious Gym Characters, Dating, Meal Prep & Banter]",
                "Session 3 Domain [Pop Culture, UFC Fight Recaps, Sports, Crypto/Tech & Late-Night Thoughts]",
            ]
            s_label = domain_labels[th_idx] if th_idx < len(domain_labels) else f"Session {th_idx + 1}"

            if session_digests:
                intra_day_lines = [f"  * {d}" for d in session_digests]
                intra_day_context = (
                    "EARLIER SESSIONS IN TODAY'S CONVERSATION (BUILD FORWARD NATURALLY):\n"
                    + "\n".join(intra_day_lines) + "\n"
                    "- CRITICAL: Do NOT restart the chat from scratch with generic openers or re-ask questions answered earlier today.\n"
                    "- Personas should chat like they have already been active in the room earlier today—transition casually, reference earlier takes, or introduce this new topic naturally."
                )
            else:
                intra_day_context = "EARLIER SESSIONS TODAY: (This is Session 1 - the opening session of today's conversation)."

            if th_idx == 0 and transcript_lines and random.random() < 0.25:
                recent_snippet = " ".join(m.get("text", "") for m in recent_msgs[-3:] if m.get("text"))[:140]
                topic_prompt = (
                    f"Natural conversational transition for {s_label}: starting from a quick casual nod to recent chatter ('{recent_snippet}'), "
                    f"then immediately pivoting to a fresh angle on: {th_theme}. NEVER repeat previous points."
                )
            else:
                topic_prompt = f"Focus for {s_label}: {th_theme}. One persona starts with a casual observation, funny take, real experience, or practical question."

            persona_summaries = []
            for c in candidates:
                dt = {}
                try:
                    dt = json.loads(c.get("details") or "{}") if isinstance(c.get("details"), str) else (c.get("details") or {})
                except Exception:
                    dt = {}
                role = dt.get("role_in_group") or dt.get("tone_and_vibe") or "Experienced regular"
                emojis = dt.get("signature_emojis") or "💀, 😂"
                emoji_habit = dt.get("emoji_habit") or "moderate"
                typing = dt.get("typing_style") or "casual phone texting"

                mem_lines = []
                if self.memory:
                    mem_ctx = self.memory.get_prompt_memory_context(c["persona_id"], chat_id=chat_id, limit=6, query_text=th_query)
                    if mem_ctx:
                        mem_lines.append(mem_ctx)
                elif hasattr(self.store, "rows"):
                    m_rows = self.store.rows(
                        "SELECT content FROM persona_memories WHERE persona_id=? AND (expires_at IS NULL OR expires_at > ?) "
                        "ORDER BY CASE retention_tier WHEN 'long' THEN 1 ELSE 2 END, salience DESC LIMIT 3",
                        (c["persona_id"], int(time.time()))
                    )
                    if m_rows:
                        mem_lines.append("Core Established Facts:\n" + "\n".join(f"  * {mr['content']}" for mr in m_rows))
                mem_str = ("\n  " + "\n  ".join(mem_lines)) if mem_lines else ""

                c_dt = {}
                if c.get("details"):
                    try:
                        c_dt = json.loads(c["details"]) if isinstance(c["details"], str) else c["details"]
                    except Exception:
                        c_dt = {}

                temp_res = ai.resolve_persona_temperament(
                    name=c.get("persona_name"),
                    details=c_dt,
                    prompt=c.get("prompt")
                )
                c_tier = temp_res["tier"]
                if c_tier == "aggressive_low_patience":
                    c_temperament = "RAW & LOW-PATIENCE INTERNET VERNACULAR (Zero filter, roasts dumb takes/questions, cuts through nonsense with sharp dismissive internet slang)"
                elif c_tier == "dry_sarcastic":
                    c_temperament = "DRY & SKEPTICAL OBSERVER (Deadpan, rolls eyes at hype, weary one-liners, sarcastic reality checks)"
                else:
                    c_temperament = "CHILL & GROUNDED PRACTICAL PEER (Calm, knowledgeable, explains practical details without corporate tone)"

                demo_info = ai.resolve_persona_demographics(
                    name=c.get("persona_name"),
                    details=c_dt,
                    prompt=c.get("prompt")
                )
                p_hash = abs(hash(str(c.get("persona_name") or c["persona_id"]))) % 4
                if p_hash == 0:
                    c_cadence = "Single-bubble purist (always 1 clean message, never splits into multiple bubbles)"
                elif p_hash == 1:
                    c_cadence = "Concise & direct (1 message usually, 5-12 words, rare follow-up)"
                elif p_hash == 2:
                    c_cadence = "Conversational (occasionally sends 2 short bubbles with ' ||| ' when reacting then expanding)"
                else:
                    c_cadence = "Terse lurker (1-6 words or emoji reaction only)"
                persona_summaries.append(
                    f"- ID: {c['persona_id']}, AccountID: {c['account_id']}, Name: {c['persona_name']}\n"
                    f"  Demographics & Identity: [{demo_info['gender'].upper()}, {demo_info['age_bracket'].upper()}] - {demo_info['vibe']}\n"
                    f"  Vibe/Role: {role}\n"
                    f"  Temperament & Speech Profile: [{c_tier.upper()}] - {c_temperament}\n"
                    f"  Knowledge & Realism Boundaries: {demo_info['knowledge_rule']}\n"
                    f"  Signature Emojis: {emojis} (habit: {emoji_habit})\n"
                    f"  Typing: {typing}\n"
                    f"  Texting Rhythm: {c_cadence}\n"
                    f"  Brief: {c.get('prompt', '')[:140]}...{mem_str}"
                )
                proto_card = self.store.get_persona_protocol_card(c.get('persona_id'))
                if proto_card:
                    persona_summaries[-1] += chr(10) + '  ' + str(proto_card)

            persona_str = "\n\n".join(persona_summaries)
            rel_graph = format_relationship_graph(candidates)
            persona_str += f"\n\nINTER-PERSONA RELATIONSHIP DYNAMICS:\n{rel_graph}\n(Personas banter, reference shared history, and chat like close peers without using @tags!)"

            system_prompt = (
                "You orchestrate realistic, organic human conversations in a specialist private Telegram group.\n"
                "You write authentic, casual group chat exchanges between real people texting from their phones.\n\n"
                "CRITICAL TELEGRAM TEXTING RULES (ZERO AI DIALECT):\n"
                "- TEXT LIKE REAL PEOPLE ON THEIR PHONES: Use lowercase by default, casual phrasing, contractions, and minimal punctuation.\n"
                "- ZERO GREETINGS OR FORMAL INTRODUCTIONS: Never say 'Hey guys', 'What is up everyone', or 'Has anyone tried X?'. Real people jump straight in with an observation, update, or quick complaint ('pip on this new batch is nonexistent', 'bench felt heavy as hell today', 'finally broke 200lbs').\n"
                "- ASYMMETRICAL CHAT FLOW (ANTI-PANEL & ANTI-Q&A LOOP):\n"
                "  * Real Telegram groups NEVER operate like an orderly Q&A panel, support ticket, or round-table seminar.\n"
                "  * Do NOT have every member take turns validating or offering helpful advice on the same topic.\n"
                "  * At MOST ONE member gives practical advice. Other members react naturally: 1-4 word dismissive banter ('cap', 'nah fr', 'rip', 'lmao why', 'definitely not'), roasting, dry skepticism, or fixating on a minor incidental detail ('wait what gym?', 'who said that bs').\n"
                "  * Questions frequently go UNANSWERED or are deflected. Real people leave questions hanging and change the subject.\n"
                "  * Mix in raw 1-4 word reactive turns. Real group texting is heavily weighted toward quick fragments mixed with occasional sentences.\n"
                "- NATURAL TOPIC DRIFT & SPLINTERING:\n"
                "  * In a thread of 8-16 messages, conversation does NOT stay locked to one single topic from start to finish.\n"
                "  * Around turn 4-7, let the conversation naturally splinter or drift: someone drops an off-topic side remark (what they are eating, terrible gym music, bad traffic, sleep/energy complaint, work frustration), and members banter about that before wrapping up.\n"
                "- NEVER DEFINE COMPOUNDS OR RECITE DOSING RECIPES:\n"
                "  * Everyone here is already an experienced regular. Never explain basic concepts or lecture like an encyclopedia.\n"
                "  * Talk about lived gym realities: recovery, joint soreness, workout intensity, sleep quality, appetite, or personal gym progress ('elbow feels 100x better', 'down 3lbs this week', 'sleep has been brutal'). NEVER recite clinical dosages or formulaic recipe templates ('running X mcg daily').\n"
                "- ZERO ARTIFICIAL VALIDATION OR OPENERS:\n"
                "  * NEVER open messages with 'I agree', 'Good point', 'Spot on', 'Exactly', 'Solid advice', or '100% true'.\n"
                "  * Jump straight into the raw thought, disagreement, roast, or personal update.\n"
                "- SHORT, PUNCHY MESSAGING CADENCE:\n"
                "  * 70-80% of messages are 3 to 12 words! Real people do not write long paragraphs on Telegram.\n"
                "  * Quick reactions ('same honestly', 'clean numbers', 'definitely not', 'which compound?', 'lol nah').\n"
                "  * Occasional 1-2 sentence elaboration when sharing personal experience.\n"
                "- ASYMMETRICAL PERSONALITY FRICTION:\n"
                "  * Low-patience members roast bad form, call out overcomplication ('stop overthinking basic shit and just train', 'who told you that garbage💀').\n"
                "  * Dry/sarcastic members roll their eyes at hype ('sure bro whatever you say', 'rip your sleep schedule').\n"
                "  * Grounded peers give practical, level-headed tips without corporate fluff.\n"
                "- NATURAL GROUP DYNAMICS:\n"
                "  * Feature 2 to 4 active members naturally conversing in this exchange.\n"
                "  * Members chime in, share personal updates, or roast bad ideas without forcing an artificial roll call.\n"
                "- MINIMAL EMOJI USAGE (REAL PHONE TEXTING):\n"
                "  * 85%+ of messages must contain ZERO emojis! Do NOT pepper emojis into every message.\n"
                "  * Native emoji reactions must be rare (under 3-5% of turns at most, e.g. a rare 👍 or 😂 reaction to a punchline).\n"
                "  * Absolutely NEVER use AI emojis: NO 💡, ✨, 📊, 🧪, 📉, 🩸, 💅, 🧠, 🤖, 🚀.\n"
                "- CONTINUITY & ZERO REPETITION MANDATE:\n"
                f"{forbidden_block}\n"
                "- Do NOT re-ask questions that were already discussed in recent chat or earlier sessions today. Advance the discussion naturally.\n"
                "- ZERO @TAGS OR USERNAMES: Address someone ONLY by setting reply_to_step.\n\n"
                "OUTPUT FORMAT: Return ONLY a valid JSON array of message objects matching this exact format:\n"
                "[\n"
                "  {\n"
                "    \"persona_id\": <int>,\n"
                "    \"account_id\": <int>,\n"
                "    \"sender_name\": \"<string>\",\n"
                "    \"reply_to_step\": <int or null>,\n"
                "    \"text\": \"<exact telegram message text>\",\n"
                "    \"reaction\": \"<optional emoji or null>\"\n"
                "  }\n"
                "]"
            )

            user_prompt = (
                f"COMMUNITY GROUP: {group_title}\n"
                f"ABOUT: {group_about}\n"
                f"DOMAIN TOPICS: {group_domain}\n\n"
                f"RECENT CONVERSATION HISTORY & ESTABLISHED CONTEXT:\n{condensed_history}\n\n"
                f"{intra_day_context}\n\n"
                f"AVAILABLE PERSONAS IN THIS CHAT:\n{persona_str}\n\n"
                f"CONVERSATIONAL FOCUS: {topic_prompt}\n\n"
                f"TASK:\n"
                f"Generate a {th_count}-message fast-paced authentic Telegram group chat thread for {s_label}.\n"
                f"Text like real lifters on mobile phones: fast, punchy, lowercase default, blunt banter, zero AI disclaimers, zero Wikipedia explanations, zero Q&A seminar loops.\n"
                "Incorporate natural topic drift around turn 4-7 and authentic asymmetrical participation.\n"
                "Advance the conversation organically without repeating past points. Return strictly the JSON array."
            )

            th_items = []
            try:
                raw_ai, meta = await self.ai.chat_with_meta(settings, system_prompt, user_prompt, temperature=0.78)
                raw = raw_ai.strip()
                raw = re.sub(r"^```(?:json)?", "", raw).strip()
                raw = re.sub(r"```$", "", raw).strip()
                match = re.search(r"\[\s*\{.*\}\s*\]", raw, re.DOTALL)
                if match:
                    clean_text = re.sub(r",\s*([\]\}])", r"\1", match.group(0))
                    try:
                        th_items = json.loads(clean_text)
                    except Exception:
                        pass
                if not th_items:
                    for m_obj in re.finditer(r"\{[^{}]*\}", raw):
                        try:
                            clean_obj = re.sub(r",\s*([\]\}])", r"\1", m_obj.group(0))
                            th_items.append(json.loads(clean_obj))
                        except Exception:
                            pass
                now_ts = int(time.time())
                p_tok = meta.get("prompt_tokens", 0)
                c_tok = meta.get("completion_tokens", 0)
                tot_tok = p_tok + c_tok
                cost_est = (tot_tok / 1000.0) * 0.0003
                self.store.q("""
                    INSERT INTO usage_logs(timestamp, event_type, chat_id, tokens_prompt, tokens_completion,
                                           tokens_total, latency_ms, model, cost_est)
                    VALUES(?,?,?,?,?,?,?,?,?)
                """, (now_ts, "ai_batch", chat_id, p_tok, c_tok, tot_tok, meta.get("latency_ms", 0),
                      meta.get("model", ""), cost_est))
            except Exception as e:
                log.error("AI batch generation error for session %d in group %s: %s", th_idx + 1, chat_id, e)

            if th_items:
                try:
                    for it in th_items:
                        if it.get("text") and not it.get("reaction"):
                            cleaned = self.ai.format_telegram_message(it["text"], names=cand_names, max_chars=360)
                            pid = it.get("persona_id")
                            aid = it.get("account_id")
                            c_item = next((c for c in candidates if (pid and c.get("persona_id") == pid) or (aid and c.get("account_id") == aid)), None)
                            it["text"] = maybe_inject_typo_correction(cleaned, candidate=c_item)
                except Exception as e:
                    log.warning("Formatting error in session %d: %s", th_idx + 1, e)
                sessions_items.append(th_items)

                # Generate compact algorithmic digest of this session for subsequent sessions and archiving
                s_speakers = list(dict.fromkeys(it.get("sender_name") for it in th_items if it.get("sender_name")))[:4]
                s_snippets = [it.get("text", "").replace("\n", " ")[:60] for it in th_items if it.get("text") and not it.get("text", "").startswith("[reaction:")][:3]
                snippet_str = "; ".join(f"'{s}'" for s in s_snippets)
                session_digests.append(f"{s_label} [{th_theme[:40]}]: with {', '.join(s_speakers)}. Highlights: {snippet_str}")

        now_ts = int(time.time())
        created_items = []
        cand_usage = { (c["persona_id"], c["account_id"]): 0 for c in candidates }

        session_start_min = random.uniform(0.10, 0.25)
        for s_idx, s_items in enumerate(sessions_items):
            clusters = []
            rem = len(s_items)
            while rem > 0:
                c_size = min(rem, random.choices([1, 2, 3, 4], weights=[0.15, 0.40, 0.35, 0.10])[0])
                clusters.append(c_size)
                rem -= c_size

            target_cluster_gap_min = max(3.0, min(7.5, 45.0 / max(len(clusters), 1)))
            session_offsets = []
            cur_time = session_start_min

            for c_idx, c_size in enumerate(clusters):
                if c_idx > 0:
                    gap = target_cluster_gap_min * random.uniform(0.80, 1.15)
                    cur_time += gap
                m_time = cur_time
                for msg_i in range(c_size):
                    if msg_i > 0:
                        m_time += random.uniform(0.18, 0.55)
                    session_offsets.append(m_time)

            # Natural cluster validation: keep items mapped to authentic personas without identity swapping
            pass

            session_created = []
            for item_idx, item in enumerate(s_items):
                pid = item.get("persona_id")
                aid = item.get("account_id")
                sname = (item.get("sender_name") or "").lower()

                cand_match = [c for c in candidates if c["persona_id"] == pid and c["account_id"] == aid]
                if not cand_match and pid:
                    cand_match = [c for c in candidates if c["persona_id"] == pid]
                if not cand_match and aid:
                    cand_match = [c for c in candidates if c["account_id"] == aid]
                if not cand_match and sname:
                    cand_match = [c for c in candidates if sname in (c.get("persona_name") or "").lower() or sname in (c.get("account_name") or "").lower()]
                if not cand_match:
                    sched_m = session_offsets[item_idx] if item_idx < len(session_offsets) else (item_idx * 3.0)
                    target_ts = now_ts + int(sched_m * 60)
                    awake_cands = [c for c in candidates if self.is_candidate_awake(c, ts=target_ts)]
                    pool = awake_cands if awake_cands else candidates
                    least_used = sorted(pool, key=lambda c: cand_usage.get((c["persona_id"], c["account_id"]), 0))[0]
                    cand_match = [least_used]

                chosen_cand = cand_match[0]
                pid = chosen_cand["persona_id"]
                aid = chosen_cand["account_id"]
                sender_name = chosen_cand["persona_name"]
                cand_usage[(pid, aid)] = cand_usage.get((pid, aid), 0) + 1

                text = (item.get("text") or "").strip()
                reaction = (item.get("reaction") or "").strip()
                if not text and not reaction:
                    continue
                if not text and reaction:
                    text = f"[reaction: {reaction}]"
                elif text:
                    text = self.ai.format_telegram_message(text, names=cand_names, max_chars=360)
                    text = sanitize_banned_tropes(text)
                    if not text:
                        continue

                scheduled_min = session_offsets[item_idx] if item_idx < len(session_offsets) else (session_start_min + (item_idx * 2))
                jitter_sec = random.randint(-5, 8)
                scheduled_ts = max(now_ts + 5, now_ts + int(scheduled_min * 60) + jitter_sec)

                rep_step = item.get("reply_to_step")
                parent_item_id = None
                if rep_step is not None and isinstance(rep_step, int) and 0 <= rep_step < len(session_created):
                    parent_item_id = session_created[rep_step]["id"]
                elif item_idx > 0 and (session_offsets[item_idx] - session_offsets[item_idx-1]) < 8.0 and session_created:
                    parent_item_id = session_created[-1]["id"]

                cur_item = self.store.q(
                    "INSERT INTO daily_batch_items(batch_id, chat_id, account_id, persona_id, sender_name, text, scheduled_ts, status, created_at, reply_to_item_id) "
                    "VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                    (batch_id, chat_id, aid, pid, sender_name, text, scheduled_ts, now_ts, parent_item_id)
                )
                item_id = cur_item.lastrowid

                c_burst = 25
                if pid:
                    for cand_item in candidates:
                        if cand_item.get("persona_id") == pid:
                            try:
                                dt_val = json.loads(cand_item["details"]) if isinstance(cand_item.get("details"), str) else (cand_item.get("details") or {})
                                c_burst = int(dt_val.get("burstiness") or dt_val.get("burstiness_percent") or 25)
                            except Exception:
                                c_burst = 25
                            break
                batch_bubbles = self.ai.split_burst_bubbles(text, names=cand_names, burstiness_pct=c_burst)
                if not batch_bubbles:
                    batch_bubbles = [text] if text else []
                batch_bubbles = [b for b in batch_bubbles if not (b.strip().startswith("[reaction:") and b.strip().endswith("]"))]
                actual_text = "" if (reaction and not item.get("text")) else text
                self.store.enqueue(
                    "batch_scheduled_send",
                    chat_id,
                    {
                        "batch_item_id": item_id,
                        "account_id": aid,
                        "persona_id": pid,
                        "text": actual_text,
                        "bubbles": batch_bubbles,
                        "reaction": reaction if reaction else None,
                        "reply_to_item_id": parent_item_id
                    },
                    dedupe_key=f"batch_send:{item_id}",
                    delay=max(0, scheduled_ts - now_ts)
                )

                created_item_dict = {
                    "id": item_id,
                    "persona_id": pid,
                    "account_id": aid,
                    "sender_name": sender_name,
                    "scheduled_ts": scheduled_ts,
                    "text": text,
                    "status": "pending",
                    "reply_to_item_id": parent_item_id
                }
                session_created.append(created_item_dict)
                created_items.append(created_item_dict)

            last_session_end = session_offsets[-1] if session_offsets else session_start_min
            if len(sessions_items) >= 3:
                session_start_min = last_session_end + random.uniform(8.0, 16.0)
            else:
                session_start_min = last_session_end + random.uniform(10.0, 20.0)

        if hasattr(self.store, "save_condensed_batch_summary") and session_digests:
            self.store.save_condensed_batch_summary(chat_id, batch_id, today, session_digests)

        if not created_items:
            raise RuntimeError("Failed to generate any valid messages across batch sessions")

        return {
            "batch_id": batch_id,
            "chat_id": chat_id,
            "count": len(created_items),
            "date": today,
            "items": created_items
        }

    def get_status(self):
        """Returns today's batch schedule, execution metrics, and scout configuration."""
        today = self.today_str()
        batches = self.store.rows("SELECT * FROM daily_batches WHERE date_str=? ORDER BY id DESC", (today,))
        all_items = self.store.rows(
            "SELECT dbi.*, g.title as group_title, p.name as persona_name, a.name as account_name "
            "FROM daily_batch_items dbi "
            "LEFT JOIN groups g ON g.chat_id=dbi.chat_id "
            "LEFT JOIN personas p ON p.id=dbi.persona_id "
            "LEFT JOIN accounts a ON a.id=dbi.account_id "
            "WHERE COALESCE(dbi.created_at, dbi.scheduled_ts, 0) >= ? "
            "ORDER BY dbi.scheduled_ts ASC",
            (int(time.time()) - 86400,)
        )

        total = len(all_items)
        pending = sum(1 for x in all_items if x["status"] == "pending")
        sent = sum(1 for x in all_items if x["status"] == "sent")
        postponed = sum(1 for x in all_items if x["status"] == "postponed")
        failed = sum(1 for x in all_items if x["status"] == "failed")
        cancelled = sum(1 for x in all_items if x["status"] == "cancelled")

        # Find next scheduled execution
        now_ts = int(time.time())
        next_items = [x for x in all_items if x["status"] == "pending" and x["scheduled_ts"] >= now_ts]
        next_ts = next_items[0]["scheduled_ts"] if next_items else None

        # Check scout account
        scout_aid = self.store.get("scout_account_id")
        scout_info = None
        if scout_aid:
            acc = self.store.rows("SELECT id, name, phone, username, active FROM accounts WHERE id=?", (scout_aid,))
            if acc:
                scout_info = acc[0]
        if not scout_info:
            acc = self.store.rows("SELECT id, name, phone, username, active FROM accounts WHERE active=1 ORDER BY id ASC LIMIT 1")
            if acc:
                scout_info = acc[0]

        auto_pilot = bool(self.store.get("daily_batch_auto_pilot", True))
        batch_count = int(self.store.get("batch_messages_count", 48))
        human_pause = max(0.5, round(int(self.store.get("batch_human_pause_seconds", 30)) / 60.0, 1))
        last_human = self.store.get("last_human_event")

        return {
            "date": today,
            "batches_count": len(batches),
            "auto_pilot": auto_pilot,
            "batch_messages_count": batch_count,
            "human_pause_minutes": human_pause,
            "session_interval_seconds": int(self.store.get("batch_session_interval_seconds", 90)),
            "last_human_event": last_human,
            "stats": {
                "total": total,
                "pending": pending,
                "sent": sent,
                "postponed": postponed,
                "failed": failed,
                "cancelled": cancelled
            },
            "next_scheduled_ts": next_ts,
            "items": all_items,
            "scout_account": scout_info,
            "live_ai_mode": "Humans Only (Bot replies strictly filtered)"
        }

    def clear_today(self, chat_id=None):
        """Cancels and purges all pending, postponed, and failed scheduled batch messages for today."""
        today = self.today_str()
        if chat_id:
            items = self.store.rows(
                "SELECT id FROM daily_batch_items WHERE chat_id=? AND status IN ('pending', 'postponed', 'failed')", (int(chat_id),)
            )
            for it in items:
                self.store.q("DELETE FROM daily_batch_items WHERE id=?", (it["id"],))
                self.store.q("DELETE FROM queue WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("DELETE FROM queue WHERE kind='batch_scheduled_send' AND chat_id=? AND status IN ('pending', 'postponed', 'failed')", (int(chat_id),))
        else:
            items = self.store.rows("SELECT id FROM daily_batch_items WHERE status IN ('pending', 'postponed', 'failed')")
            for it in items:
                self.store.q("DELETE FROM daily_batch_items WHERE id=?", (it["id"],))
                self.store.q("DELETE FROM queue WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("DELETE FROM queue WHERE kind='batch_scheduled_send' AND status IN ('pending', 'postponed', 'failed')")
        return len(items)

    def clear_failed(self, chat_id=None):
        """Specifically removes and purges all failed batch items and failed queue entries."""
        if chat_id:
            items = self.store.rows("SELECT id FROM daily_batch_items WHERE chat_id=? AND status='failed'", (int(chat_id),))
            for it in items:
                self.store.q("DELETE FROM daily_batch_items WHERE id=?", (it["id"],))
                self.store.q("DELETE FROM queue WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("DELETE FROM queue WHERE kind='batch_scheduled_send' AND chat_id=? AND status='failed'", (int(chat_id),))
        else:
            items = self.store.rows("SELECT id FROM daily_batch_items WHERE status='failed'")
            for it in items:
                self.store.q("DELETE FROM daily_batch_items WHERE id=?", (it["id"],))
                self.store.q("DELETE FROM queue WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("DELETE FROM queue WHERE status='failed'")
        return len(items)

    def trigger_next_now(self):
        """Forces the next pending or postponed scheduled message to send immediately."""
        now_ts = int(time.time())
        pending = self.store.rows(
            "SELECT id, chat_id FROM daily_batch_items WHERE status IN ('pending', 'postponed') ORDER BY scheduled_ts ASC LIMIT 1"
        )
        if not pending:
            return None
        it_id = pending[0]["id"]
        self.store.q("UPDATE daily_batch_items SET status='pending', scheduled_ts=? WHERE id=?", (now_ts, it_id))
        self.store.q("UPDATE queue SET status='pending', not_before=? WHERE dedupe_key=?", (now_ts, f"batch_send:{it_id}"))
        return it_id

