from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

try:
    from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError
except ImportError:
    # Fallback to stdlib shim when openai package is unavailable
    import sys, os as _os
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    from openai_shim import APIConnectionError, APITimeoutError, OpenAI, RateLimitError  # type: ignore


SYSTEM_PROMPT = """You are a certified technical translator specializing in GHS Safety Data Sheets (SDS/MSDS).
Use official Russian chemical safety terminology.
Translate as a regulatory document, not conversational text.
Preserve meaning exactly. Never simplify safety instructions."""

VALIDATION_PROMPT = """Review the Russian translation of a GHS/MSDS text block.

Fix only if needed.
Check for:
- meaning loss
- wrong subject reference
- informal or conversational language
- non-standard MSDS terminology

Return only the corrected Russian translation."""

CODE_PATTERNS = (
    r"\bH\d{3}\b",
    r"\bP\d{3}\b",
    r"\bEUH\d+\b",
    r"\b\d{2,7}-\d{2}-\d\b",
)

PROTECTED_TOKEN_PATTERNS = (
    r"\bH\d{3}\b",
    r"\bP\d{3}\b",
    r"\bEUH\d+\b",
    r"\b\d{2,7}-\d{2}-\d\b",
    r"\b\d+(?:\.\d+)?\s*(?:ppm|mg/m³|mg/m3|°C|°F|%)\b",
)

SOURCE_GLOSSARY = {
    "Store locked up": "Хранить под замком",
    "May cause respiratory irritation": "Может вызывать раздражение дыхательных путей",
    "May cause drowsiness or dizziness": "Может вызывать сонливость или головокружение",
    "Toxic to aquatic life with long lasting effects": "Токсично для водной среды, длительное действие",
    "May be fatal if swallowed and enters airways": "Может быть смертельно при проглатывании и попадании в дыхательные пути",
    "May be fatal if swallowed and enters airways.": "Может быть смертельно при проглатывании и попадании в дыхательные пути.",
    "Combustible Liquid": "Горючая жидкость",
    "Harmful if swallowed": "Вредно при проглатывании",
    "Harmful if swallowed.": "Вредно при проглатывании.",
    "Causes serious eye damage": "Вызывает серьезное повреждение глаз",
    "Causes serious eye damage.": "Вызывает серьезное повреждение глаз.",
    "Repeated exposure may cause skin dryness or cracking": "Повторное воздействие может вызвать сухость или растрескивание кожи",
    "Repeated exposure may cause skin dryness or cracking.": "Повторное воздействие может вызвать сухость или растрескивание кожи.",
    "If medical advice is needed, have product container or label at hand": "При необходимости обращения за медицинской помощью иметь при себе упаковку продукта или этикетку",
    "If medical advice is needed, have product container or label at hand.": "При необходимости обращения за медицинской помощью иметь при себе упаковку продукта или этикетку.",
    "Keep out of reach of children": "Хранить в недоступном для детей месте",
    "Keep out of reach of children.": "Хранить в недоступном для детей месте.",
    "Read carefully and follow all instructions": "Внимательно прочитать и выполнять все инструкции",
    "Read carefully and follow all instructions.": "Внимательно прочитать и выполнять все инструкции.",
    "Wear protective gloves/protective clothing/eye protection/face protection": "Использовать защитные перчатки/защитную одежду/средства защиты глаз/средства защиты лица",
    "Wear protective gloves/protective clothing/eye protection/face protection.": "Использовать защитные перчатки/защитную одежду/средства защиты глаз/средства защиты лица.",
    "Wash all exposed external body areas thoroughly after handling": "После работы тщательно вымыть все открытые участки тела",
    "Wash all exposed external body areas thoroughly after handling.": "После работы тщательно вымыть все открытые участки тела.",
    "Do not eat, drink or smoke when using this product": "Не есть, не пить и не курить при использовании этого продукта",
    "Do not eat, drink or smoke when using this product.": "Не есть, не пить и не курить при использовании этого продукта.",
    "IF SWALLOWED: Immediately call a POISON CENTER/doctor/physician/first aider": "ПРИ ПРОГЛАТЫВАНИИ: немедленно обратиться в ТОКСИКОЛОГИЧЕСКИЙ ЦЕНТР/к врачу/медику/лицу, оказывающему первую помощь",
    "IF SWALLOWED: Immediately call a POISON CENTER/doctor/physician/first aider.": "ПРИ ПРОГЛАТЫВАНИИ: немедленно обратиться в ТОКСИКОЛОГИЧЕСКИЙ ЦЕНТР/к врачу/медику/лицу, оказывающему первую помощь.",
    "Do NOT induce vomiting": "НЕ вызывать рвоту",
    "Do NOT induce vomiting.": "НЕ вызывать рвоту.",
    "Do NOT induce vomiting. If more than 15 mins from Doctor, INDUCE VOMITING (if conscious).": "НЕ вызывать рвоту. Если врач недоступен более 15 минут, вызвать рвоту (если пострадавший в сознании).",
    "IF IN EYES: Rinse cautiously with water for several minutes. Remove contact lenses, if present and easy to do. Continue rinsing": "ПРИ ПОПАДАНИИ В ГЛАЗА: осторожно промывать водой в течение нескольких минут. Снять контактные линзы, если они есть и это легко сделать. Продолжить промывание",
    "IF IN EYES: Rinse cautiously with water for several minutes. Remove contact lenses, if present and easy to do. Continue rinsing.": "ПРИ ПОПАДАНИИ В ГЛАЗА: осторожно промывать водой в течение нескольких минут. Снять контактные линзы, если они есть и это легко сделать. Продолжить промывание.",
    "IF SWALLOWED: Call a POISON CENTER/doctor/physician/first aider if you feel unwell": "ПРИ ПРОГЛАТЫВАНИИ: при плохом самочувствии обратиться в ТОКСИКОЛОГИЧЕСКИЙ ЦЕНТР/к врачу/медику/лицу, оказывающему первую помощь",
    "IF SWALLOWED: Call a POISON CENTER/doctor/physician/first aider if you feel unwell.": "ПРИ ПРОГЛАТЫВАНИИ: при плохом самочувствии обратиться в ТОКСИКОЛОГИЧЕСКИЙ ЦЕНТР/к врачу/медику/лицу, оказывающему первую помощь.",
    "Rinse mouth": "Прополоскать рот",
    "Rinse mouth.": "Прополоскать рот.",
    "Risk of explosion if heated under confinement": "Опасность взрыва при нагревании в замкнутом пространстве",
    "Risk of explosion if heated under confinement.": "Опасность взрыва при нагревании в замкнутом пространстве.",
    "Contains 1,2-benzisothiazoline-3-one. May produce an allergic reaction.": "Содержит 1,2-бензизотиазолин-3-он. Может вызвать аллергическую реакцию.",
    "Contains subtilisins. May produce an allergic reaction.": "Содержит субтилизины. Может вызвать аллергическую реакцию.",
    "Safety data sheet available on request": "Паспорт безопасности предоставляется по запросу",
    "Safety data sheet available on request.": "Паспорт безопасности предоставляется по запросу.",
    "1.3 Details of the supplier of the safety data sheet": "1.3 Данные поставщика",
    ":\nUse extinguishing measures that are appropriate to local cir-\ncumstances and the surrounding environment.\nSpecial protective equipment\nfor firefighters": ":\nИспользовать меры пожаротушения, соответствующие местным условиям и окружающей обстановке.\nСпециальные средства защиты пожарных",
    ":\nProper protective equipment including chemical resistant\ngloves are to be worn; chemical resistant suit is indicated if\nlarge contact with spilled product is expected. Self-Contained\nBreathing Apparatus must be worn when approaching a fire in\na confined space. Select fire fighter's clothing approved to\nrelevant Standards (e.g. Europe: EN469).": ":\nИспользовать соответствующие средства защиты, включая химически стойкие перчатки; при значительном контакте с проливом показан химически стойкий костюм. В закрытых помещениях использовать автономный дыхательный аппарат. Одежда пожарных должна соответствовать применимым стандартам, например EN469.",
    "Extremely flammable gas": "Чрезвычайно легковоспламеняющийся газ",
    "Extremely flammable gas.": "Чрезвычайно легковоспламеняющийся газ.",
    "May cause or intensify fire; oxidiser": "Может вызвать или усилить пожар; окислитель",
    "May cause or intensify fire; oxidiser.": "Может вызвать или усилить пожар; окислитель.",
    "Contains gas under pressure; may explode if heated": "Содержит газ под давлением; при нагревании может взорваться",
    "Contains gas under pressure; may explode if heated.": "Содержит газ под давлением; при нагревании может взорваться.",
    "Contains refrigerated gas; may cause cryogenic burns or injury": "Содержит охлажденный газ; может вызвать криогенные ожоги или травмы",
    "Contains refrigerated gas; may cause cryogenic burns or injury.": "Содержит охлажденный газ; может вызвать криогенные ожоги или травмы.",
    "May be corrosive to metals": "Может вызывать коррозию металлов",
    "May be corrosive to metals.": "Может вызывать коррозию металлов.",
    "Toxic if swallowed": "Токсично при проглатывании",
    "Toxic if swallowed.": "Токсично при проглатывании.",
    "Causes severe skin burns and eye damage": "Вызывает серьезные ожоги кожи и повреждение глаз",
    "Causes severe skin burns and eye damage.": "Вызывает серьезные ожоги кожи и повреждение глаз.",
    "Causes skin irritation": "Вызывает раздражение кожи",
    "Causes skin irritation.": "Вызывает раздражение кожи.",
    "May cause an allergic skin reaction": "Может вызвать аллергическую кожную реакцию",
    "May cause an allergic skin reaction.": "Может вызвать аллергическую кожную реакцию.",
    "Causes serious eye irritation": "Вызывает серьезное раздражение глаз",
    "Causes serious eye irritation.": "Вызывает серьезное раздражение глаз.",
    "Toxic if inhaled": "Токсично при вдыхании",
    "Toxic if inhaled.": "Токсично при вдыхании.",
    "May cause allergy or asthma symptoms or breathing difficulties if inhaled": "При вдыхании может вызвать симптомы аллергии или астмы либо затруднение дыхания",
    "May cause allergy or asthma symptoms or breathing difficulties if inhaled.": "При вдыхании может вызвать симптомы аллергии или астмы либо затруднение дыхания.",
    "Suspected of causing cancer": "Предположительно вызывает рак",
    "Suspected of causing cancer.": "Предположительно вызывает рак.",
    "Causes damage to organs through prolonged or repeated exposure. (Nervous system) (Inhalation)": "Вызывает поражение органов при длительном или повторном воздействии. (Нервная система) (Вдыхание)",
    "Causes damage to organs through prolonged or repeated exposure. (Nervous system) (Inhalation).": "Вызывает поражение органов при длительном или повторном воздействии. (Нервная система) (Вдыхание).",
    "Very toxic to aquatic life": "Очень токсично для водных организмов",
    "Very toxic to aquatic life.": "Очень токсично для водных организмов.",
    "Very toxic to aquatic life with long lasting effects": "Очень токсично для водных организмов с долгосрочными последствиями",
    "Very toxic to aquatic life with long lasting effects.": "Очень токсично для водных организмов с долгосрочными последствиями.",
    "Toxic to aquatic life with long lasting effects.": "Токсично для водных организмов с долгосрочными последствиями.",
    "Harmful to aquatic life with long lasting effects": "Вредно для водных организмов с долгосрочными последствиями",
    "Harmful to aquatic life with long lasting effects.": "Вредно для водных организмов с долгосрочными последствиями.",
    "Keep away from heat, hot surfaces, sparks, open flames and other ignition sources. No smoking.": "Держать вдали от источников тепла, горячих поверхностей, искр, открытого пламени и других источников воспламенения. Не курить.",
    "Keep away from clothing and other combustible materials": "Держать вдали от одежды и других горючих материалов",
    "Keep away from clothing and other combustible materials.": "Держать вдали от одежды и других горючих материалов.",
    "Keep only in original packaging": "Хранить только в оригинальной упаковке",
    "Keep only in original packaging.": "Хранить только в оригинальной упаковке.",
    "Keep valves and fittings free from oil and grease": "Не допускать попадания масла и смазки на клапаны и фитинги",
    "Keep valves and fittings free from oil and grease.": "Не допускать попадания масла и смазки на клапаны и фитинги.",
    "Do not breathe mist/vapours/spray": "Не вдыхать туман/пары/аэрозоль",
    "Do not breathe mist/vapours/spray.": "Не вдыхать туман/пары/аэрозоль.",
    "Use only outdoors or in a well-ventilated area": "Использовать только на открытом воздухе или в хорошо проветриваемом помещении",
    "Use only outdoors or in a well-ventilated area.": "Использовать только на открытом воздухе или в хорошо проветриваемом помещении.",
    "Avoid release to the environment": "Избегать попадания в окружающую среду",
    "Avoid release to the environment.": "Избегать попадания в окружающую среду.",
    "IF SWALLOWED: Rinse mouth. Do NOT induce vomiting.": "ПРИ ПРОГЛАТЫВАНИИ: прополоскать рот. НЕ вызывать рвоту.",
    "IF SWALLOWED: Rinse mouth. Do NOT induce vomiting. If more than 15 mins from Doctor, INDUCE VOMITING (if conscious).": "ПРИ ПРОГЛАТЫВАНИИ: прополоскать рот. НЕ вызывать рвоту. Если врач недоступен более 15 минут, вызвать рвоту (если пострадавший в сознании).",
    "IF ON SKIN: Wash with plenty of soap and water.": "ПРИ ПОПАДАНИИ НА КОЖУ: промыть большим количеством воды с мылом.",
    "IF ON SKIN (or hair): Take off immediately all contaminated clothing. Rinse skin with water [or shower].": "ПРИ ПОПАДАНИИ НА КОЖУ или волосы: немедленно снять всю загрязненную одежду. Промыть кожу водой или принять душ.",
    "IF INHALED: Remove person to fresh air and keep comfortable for breathing.": "ПРИ ВДЫХАНИИ: вынести пострадавшего на свежий воздух и обеспечить удобное для дыхания положение.",
    "Immediately call a POISON CENTER/doctor/physician/first aider.": "Немедленно обратиться в ТОКСИКОЛОГИЧЕСКИЙ ЦЕНТР/к врачу/медику/лицу, оказывающему первую помощь.",
    "Call a POISON CENTER/doctor/physician/first aider/if you feel unwell.": "При плохом самочувствии обратиться в ТОКСИКОЛОГИЧЕСКИЙ ЦЕНТР/к врачу/медику/лицу, оказывающему первую помощь.",
    "If skin irritation occurs: Get medical advice/attention.": "При раздражении кожи: обратиться за медицинской консультацией/помощью.",
    "If eye irritation persists: Get medical advice/attention.": "Если раздражение глаз не проходит: обратиться за медицинской консультацией/помощью.",
    "Take off contaminated clothing and wash it before reuse.": "Снять загрязненную одежду и выстирать ее перед повторным использованием.",
    "Wash contaminated clothing before reuse": "Выстирать загрязненную одежду перед повторным использованием",
    "Wash contaminated clothing before reuse.": "Выстирать загрязненную одежду перед повторным использованием.",
    "In case of fire: Stop leak if safe to do so.": "При пожаре: остановить утечку, если это безопасно.",
    "Leaking gas fire: Do not extinguish, unless leak can be stopped safely.": "Пожар при утечке газа: не тушить, если утечку нельзя безопасно остановить.",
    "In case of leakage, eliminate all ignition sources.": "В случае утечки устранить все источники воспламенения.",
    "Absorb spillage to prevent material damage": "Абсорбировать разлив для предотвращения материального ущерба",
    "Absorb spillage to prevent material damage.": "Абсорбировать разлив для предотвращения материального ущерба.",
    "Collect spillage": "Собрать разлив",
    "Collect spillage.": "Собрать разлив.",
    "Store in a well-ventilated place. Keep container tightly closed.": "Хранить в хорошо проветриваемом месте. Держать тару плотно закрытой.",
    "Protect from sunlight. Store in a well-ventilated place.": "Защищать от солнечного света. Хранить в хорошо проветриваемом месте.",
    "Dispose of contents/container to authorised hazardous or special waste collection point in accordance with any local regulation.": "Утилизировать содержимое/контейнер в авторизованном пункте сбора опасных или специальных отходов в соответствии с местными требованиями.",
    "Store in a well-ventilated place": "Хранить в хорошо проветриваемом месте",
    "Keep container tightly closed": "Держать контейнер плотно закрытым",
    "Keep cool": "Хранить в прохладном месте",
    "Store locked up.": "Хранить под замком.",
    "IF SWALLOWED": "ПРИ ПРОГЛАТЫВАНИИ",
    "IF IN EYES": "ПРИ ПОПАДАНИИ В ГЛАЗА",
    "IF INHALED": "ПРИ ВДЫХАНИИ",
    "TLV Basis:": "Осн. TLV:",
    "as total": "суммарн.",
    "eye & skin": "глаза/кожа",
    "No limit": "Без огранич.",
    "Mobility": "Подвижн.",
    "SCALE:\nMin/Nil=0\nLow=1\nModerate=2\nHigh=3\nExtreme=4": "ШКАЛА:\nМин=0\nНизк=1\nУмер=2\nВыс=3\nЧрезв=4",
    "Flammability\nToxicity\nBody Contact\nReactivity\nChronic": "Воспламеняемость\nТоксичность\nКонтакт с телом\nРеактивность\nХронические эффекты",
    "SUPPLIER\nCompany: Drew Marine\nAddress:\n100 South Jefferson Road\nWhippany, NJ 07891\nUnited States of America\nTelephone: 973 526- 5700.\nEmergency Tel:CHEMWATCH: from within the US and\nCanada: 877- 715- 9305 From outside the US and\nCanada: 800 2436 2255 (1- 800- CHEMCALL) or call\n613 9573 3112": "ПОСТАВЩИК\nКомпания: Drew Marine\nАдрес:\n100 South Jefferson Road\nWhippany, NJ 07891\nСоединенные Штаты Америки\nТелефон: 973 526- 5700.\nЭкстренный телефон: CHEMWATCH: в США и\nКанаде: 877- 715- 9305 За пределами США и\nКанады: 800 2436 2255 (1- 800- CHEMCALL) или по телефону\n613 9573 3112",
    "Wilhelmsen Ships Service AS": "Wilhelmsen Ships Service AS",
    "Wilhelmsen Ships Service AS*": "Wilhelmsen Ships Service AS*",
    "Wilhelmsen IT Services AS": "Wilhelmsen IT Services AS",
    "L.REACH.NOR.EN": "L.REACH.NOR.EN",
    "REGULATIONS\nRegulations for ingredients": "НОРМАТИВНАЯ ИНФОРМАЦИЯ\nНормативная информация по компонентам",
    "TOXICITY AND IRRITATION\n■Not available. Refer to individual constituents.": "ТОКСИЧНОСТЬ И РАЗДРАЖАЮЩЕЕ ДЕЙСТВИЕ\n■Недоступно. См. сведения по отдельным компонентам.",
    "CONDITIONS CONTRIBUTING TO INSTABILITY\n• Presence of incompatible materials.\n• Product is considered stable.\nFor incompatible materials - refer to Section 7 - Handling and Storage.": "УСЛОВИЯ, СПОСОБСТВУЮЩИЕ НЕСТАБИЛЬНОСТИ\n• Наличие несовместимых материалов.\n• Продукт считается стабильным.\nПо несовместимым материалам см. Раздел 7 - Обращение и хранение.",
    "PRODUCT USE\n■ Used according to manufacturer's directions.": "ПРИМЕНЕНИЕ ПРОДУКТА\n■ Использовать в соответствии с инструкциями производителя.",
    "HAZARD\nDANGER": "ОПАСНОСТЬ\nОПАСНО",
    "FIRE INCOMPATIBILITY\n■None known.": "НЕСОВМЕСТИМОСТЬ ПРИ ПОЖАРЕ\n■Неизвестна.",
    "STORAGE REQUIREMENTS\n• Store in original containers.\n• Keep containers securely sealed.": "ТРЕБОВАНИЯ К ХРАНЕНИЮ\n• Хранить в оригинальной таре.\n• Держать тару плотно закрытой.",
    "EYE\n• Safety glasses with side shields.\n• Chemical goggles.": "ГЛАЗА\n• Защитные очки с боковыми щитками.\n• Химические защитные очки.",
    "EYE\n• Chemical goggles.\n• Full face shield.": "ГЛАЗА\n• Химические защитные очки.\n• Полный лицевой щиток.",
    "OTHER\n• Overalls.\n• P.V.C. apron.\n• Barrier cream.\n• Skin cleansing cream.\n• Eye wash unit.": "ДОПОЛНИТЕЛЬНО\n• Комбинезон.\n• Фартук из ПВХ.\n• Защитный крем.\n• Очищающий крем для кожи.\n• Устройство для промывания глаз.",
    "SKIN\n■ If skin contact occurs:\n• Immediately remove all contaminated clothing, including footwear\n• Flush skin and hair with running water (and soap if available).": "КОЖА\n■ При контакте с кожей:\n• Немедленно снять всю загрязненную одежду, включая обувь.\n• Промыть кожу и волосы проточной водой (и мылом, если имеется).",
    "EYE\n■ If this product comes in contact with the eyes:\n• Immediately hold eyelids apart and flush the eye continuously with running water.\n• Ensure complete irrigation of the eye by keeping eyelids apart and away from eye and moving the eyelids by occasionally lifting the upper and lower\nlids.": "ГЛАЗА\n■ При попадании продукта в глаза:\n• Немедленно раскрыть веки и непрерывно промывать глаз проточной водой.\n• Обеспечить полное промывание глаза, удерживая веки раскрытыми и периодически приподнимая верхнее и нижнее веко.",
    "INHALED\n• If fumes or combustion products are inhaled remove from contaminated area.\n• Lay patient down. Keep warm and rested.": "ПРИ ВДЫХАНИИ\n• При вдыхании паров или продуктов горения вывести пострадавшего из загрязненной зоны.\n• Уложить пострадавшего. Согреть и обеспечить покой.",
    "INHALED\n• If fumes or combustion products are inhaled remove from contaminated area.\n• Other measures are usually unnecessary.": "ПРИ ВДЫХАНИИ\n• При вдыхании паров или продуктов горения вывести пострадавшего из загрязненной зоны.\n• Дополнительные меры обычно не требуются.",
    "SWALLOWED\n• Immediately give a glass of water.\n• First aid is not generally required. If in doubt, contact a Poisons Information Center or a doctor.": "ПРИ ПРОГЛАТЫВАНИИ\n• Немедленно дать стакан воды.\n• Первая помощь обычно не требуется. При сомнениях обратиться в токсикологический центр или к врачу.",
    "NOTES TO PHYSICIAN\n■Treat symptomatically.": "УКАЗАНИЯ ДЛЯ ВРАЧА\n■Проводить симптоматическое лечение.",
    "PHYSICAL PROPERTIES\nLiquid.\nMixes with water.": "ФИЗИЧЕСКИЕ СВОЙСТВА\nЖидкость.\nСмешивается с водой.",
    "ENGINEERING CONTROLS\n■Local exhaust ventilation usually required. If risk of overexposure exists, wear an approved respirator.\n.": "ИНЖЕНЕРНЫЕ МЕРЫ КОНТРОЛЯ\n■Обычно требуется местная вытяжная вентиляция. При риске превышения воздействия использовать утвержденный респиратор.\n.",
    "Personal Protective Equipment\nBreathing apparatus.\nGas tight chemical resistant suit.\nLimit exposure duration to 1 BA set 30 mins.": "Средства индивидуальной защиты\nАппарат защиты органов дыхания.\nГерметичный химически стойкий защитный костюм.\nОграничить продолжительность воздействия одним комплектом дыхательного аппарата до 30 мин.",
    "SUITABLE CONTAINER\n• Metal can or drum\n• Packing as recommended by manufacturer.": "ПОДХОДЯЩАЯ ТАРА\n• Металлическая канистра или барабан.\n• Упаковка в соответствии с рекомендациями производителя.",
    "This document is copyright. Apart from any fair dealing for the purposes of private study, research, review or\ncriticism, as permitted under the Copyright Act, no part may be reproduced by any process without written\npermission from CHEMWATCH. TEL (+61 3) 9572 4700.": "Документ защищен авторским правом. За исключением добросовестного использования в целях личного изучения, исследования, обзора или\nкритики, допускаемого законодательством об авторском праве, никакая часть документа не может воспроизводиться любым способом без письменного\nразрешения CHEMWATCH. ТЕЛ. (+61 3) 9572 4700.",
    "■ Classification of the preparation and its individual components has drawn on official and authoritative sources as well as independent review by\nthe Chemwatch Classification committee using available literature references.\nA list of reference resources used to assist the committee may be found at:\nwww.chemwatch.net/references.": "■ Классификация препарата и его отдельных компонентов основана на официальных и авторитетных источниках, а также на независимой оценке,\nвыполненной комитетом Chemwatch по классификации с использованием доступных литературных источников.\nСписок справочных ресурсов, использованных комитетом, приведен по адресу:\nwww.chemwatch.net/references.",
    "■ The (M)SDS is a Hazard Communication tool and should be used to assist in the Risk Assessment. Many factors determine whether the reported Hazards\nare Risks in the workplace or other settings. Risks may be determined by reference to Exposures Scenarios. Scale of use, frequency of use and current\nor available engineering controls must be considered.": "■ Паспорт безопасности (M)SDS является инструментом информирования об опасностях и должен использоваться для содействия оценке рисков. На то,\nявляются ли указанные опасности реальными рисками на рабочем месте или в иных условиях, влияет множество факторов. Риски могут определяться с\nучетом сценариев воздействия. Необходимо учитывать масштаб применения, частоту использования и действующие или доступные инженерные меры контроля.",
}

SIMPLE_LINE_MAP = {
    "Not Available": "Недоступно",
    "Not available": "Недоступно",
    "No data": "Нет данных",
    "Not Applicable": "Не применимо",
    "Not applicable": "Не применимо",
    "Product name": "Наименование продукта",
    "Chemical Name": "Химическое название",
    "Synonyms": "Синонимы",
    "Proper shipping name": "Правильное название для перевозки",
    "UN number or ID number:": "Номер ООН или ID-номер:",
    "UN Proper Shipping Name:": "Правильное транспортное наименование ООН:",
    "Proper Shipping Name:": "Правильное транспортное наименование:",
    "Transport Hazard Class(es)": "Класс(ы) транспортной опасности",
    "Transport Hazard Class(es):": "Класс(ы) транспортной опасности:",
    "Class:": "Класс:",
    "Non-dangerous goods": "Неопасный груз",
    "Label(s):": "Знак(и) опасности:",
    "EmS No.:": "N EmS:",
    "Packing Group:": "Группа упаковки:",
    "Environmental hazards:": "Опасность для окружающей среды:",
    "Special precautions for user:": "Специальные меры предосторожности для пользователя:",
    "Chemical formula": "Химическая формула",
    "Other means of identification": "Прочие способы идентификации",
    "Chemical Product Category": "Категория химического продукта",
    "Category": "Категория",
    "Sectors of Use": "Секторы применения",
    "Relevant identified uses": "Идентифицированные применения",
    "Uses advised against": "Применения, которых следует избегать",
    "Manufacturer/Supplier": "Производитель/Поставщик",
    "Manufacturer / Supplier": "Производитель/Поставщик",
    "Address": "Адрес",
    "Telephone": "Телефон",
    "Fax": "Факс",
    "Website": "Веб-сайт",
    "Email": "Почта",
    "Initial Date": "Нач. дата",
    "Revision Date": "Ред.",
    "Print Date": "Печать",
    "Other means of": "Прочие",
    "identification": "идентиф.",
    "Professional uses": "Проф. применение",
    "Use of functional fluid at industrial site": "Функц. жидкость на пром. объекте",
    "Oil Spill Dispersant": "Диспергатор разливов нефти",
    "Water-based cleaner and degreaser.": "Водный очиститель/обезжириватель.",
    "Biological drain and pipe cleaner.": "Биоочиститель сливов и труб.",
    "Emergency telephone number(s)": "Экстренный телефон",
    "Association / Organisation": "Ассоциация / Организация",
    "Emergency telephone number": "Экстренный телефон",
    "Industrial uses": "Промышленные применения",
    "Washing and cleaning products": "Моющие и чистящие средства",
    "No specific uses advised against are identified.": "Не указаны специфические применения, которых следует избегать.",
    "None": "Отсутствует",
    "LOW": "НИЗКИЙ",
    "State": "Агрегатное состояние",
    "Liquid": "Жидкость",
    "Solid": "Твердое вещество",
    "Divided solid": "Дисперсное твердое вещество",
    "Molecular Weight": "Молекулярная масса",
    "Viscosity": "Вязкость",
    "Melting Range (°F)": "Диапазон плавления (°F)",
    "Boiling Range (°F)": "Диапазон кипения (°F)",
    "Solubility in water (g/L)": "Растворимость в воде (г/л)",
    "Flash Point (°F)": "Температура вспышки (°F)",
    "pH (1% solution)": "pH (1% раствор)",
    "Decomposition Temp (°F)": "Температура разложения (°F)",
    "pH (as supplied)": "pH (как поставляется)",
    "Autoignition Temp (°F)": "Температура самовоспламенения (°F)",
    "Vapour Pressure (mmHG)": "Давление пара (мм рт. ст.)",
    "Upper Explosive Limit (%)": "Верхний предел взрываемости (%)",
    "Lower Explosive Limit (%)": "Нижний предел взрываемости (%)",
    "Specific Gravity (water=1)": "Относительная плотность (вода=1)",
    "Relative Vapor Density": "Относительная плотность пара",
    "(air=1)": "(воздух=1)",
    "Volatile Component (%vol)": "Летучая составляющая (% об.)",
    "Evaporation Rate": "Скорость испарения",
    "Source": "Источник",
    "Material": "Материал",
    "Notes": "Примечания",
    "Exposure Limits": "Пределы воздействия",
    "Occupational Exposure": "Профессиональное воздействие",
    "Permissible Exposure": "Допустимое воздействие",
    "Occupational": "Профессиональный",
    "Permissible": "Допустимый",
    "Limits": "Пределы",
    "Air Contaminants": "Загрязняющие вещества воздуха",
    "Contaminants": "Загрязняющие вещества",
    "TWA ppm": "TWA, ppm (средневзвешенная за смену)",
    "STEL ppm": "STEL, ppm (кратковременное воздействие)",
    "TWA mg/m³": "TWA, мг/м³",
    "Limit Values (TLV)": "Предельные значения (TLV)",
    "EYE": "ПРИ ПОПАДАНИИ В ГЛАЗА",
    "SKIN": "КОЖА",
    "SWALLOWED": "ПРИ ПРОГЛАТЫВАНИИ",
    "INHALED": "ПРИ ВДЫХАНИИ",
    "RESPIRATOR": "РЕСПИРАТОР",
    "Particulate": "Аэрозоль/пыль",
    "OTHER": "ДРУГОЕ",
    "ENGINEERING CONTROLS": "ИНЖЕНЕРНЫЕ МЕРЫ КОНТРОЛЯ",
    "HANDS/FEET": "РУКИ/НОГИ",
    "APPEARANCE": "ВНЕШНИЙ ВИД",
    "PHYSICAL PROPERTIES": "ФИЗИЧЕСКИЕ СВОЙСТВА",
    "Personal Protective Equipment": "Средства индивидуальной защиты",
    "Breathing apparatus.": "Аппарат защиты органов дыхания.",
    "Chemical splash suit.": "Защитный костюм от химических брызг.",
    "Gas tight chemical resistant suit.": "Герметичный химически стойкий защитный костюм.",
    "Water treatment chemical.": "Химическое средство для обработки воды.",
    "PRODUCT USE": "ПРИМЕНЕНИЕ ПРОДУКТА",
    "NOTES TO PHYSICIAN": "УКАЗАНИЯ ДЛЯ ВРАЧА",
    "POTENTIAL HEALTH EFFECTS": "ВОЗМОЖНОЕ ВОЗДЕЙСТВИЕ НА ЗДОРОВЬЕ",
    "CONDITIONS CONTRIBUTING TO INSTABILITY": "УСЛОВИЯ, СПОСОБСТВУЮЩИЕ НЕСТАБИЛЬНОСТИ",
    "REGULATIONS": "НОРМАТИВНАЯ ИНФОРМАЦИЯ",
    "Regulations for ingredients": "Нормативная информация по компонентам",
    "TOXICITY AND IRRITATION": "ТОКСИЧНОСТЬ И РАЗДРАЖАЮЩЕЕ ДЕЙСТВИЕ",
    "FIRE INCOMPATIBILITY": "НЕСОВМЕСТИМОСТЬ ПРИ ПОЖАРЕ",
    "STORAGE REQUIREMENTS": "ТРЕБОВАНИЯ К ХРАНЕНИЮ",
    "SUITABLE CONTAINER": "ПОДХОДЯЩАЯ ТАРА",
    "RECOMMENDED STORAGE METHODS": "РЕКОМЕНДУЕМЫЕ СПОСОБЫ ХРАНЕНИЯ",
    "Packing Instructions:": "Инструкции по упаковке:",
    "Special provisions:": "Специальные положения:",
    "Maximum Qty/Pack:": "Макс. количество/упаковка:",
    "Packing Group:": "Группа упаковки:",
    "Packaging: Exceptions:": "Упаковка: исключения:",
    "Address:": "Адрес:",
    "Telephone: 973 526- 5700.": "Телефон: 973 526- 5700.",
    "United States of America": "Соединенные Штаты Америки",
    "Respirators must be NIOSH approved.": "Респираторы должны быть одобрены NIOSH.",
    "observed when making a final choice.": "учитываться при окончательном выборе.",
    "Company: Drew Marine": "Компания: Drew Marine",
    "OSHA Standards - 29 CFR:": "Стандарты OSHA - 29 CFR:",
    "■ Used according to manufacturer's directions.": "■ Использовать в соответствии с инструкциями производителя.",
    "■Treat symptomatically.": "■Проводить симптоматическое лечение.",
    "■None known.": "■Неизвестна.",
    "• Chemical goggles.": "• Химические защитные очки.",
    "• Overalls.": "• Комбинезон.",
    "• P.V.C. apron.": "• Фартук из ПВХ.",
    "• Barrier cream.": "• Защитный крем.",
    "• Skin cleansing cream.": "• Очищающий крем для кожи.",
    "• Eye wash unit.": "• Устройство для промывания глаз.",
    "• Full face shield.": "• Полный лицевой щиток.",
    "• Metal can or drum": "• Металлическая канистра или барабан.",
    "• Packing as recommended by manufacturer.": "• Упаковка в соответствии с рекомендациями производителя.",
    "• Store in original containers.": "• Хранить в оригинальной таре.",
    "• Keep containers securely sealed.": "• Держать тару плотно закрытой.",
    "• If fumes or combustion products are inhaled remove from contaminated area.": "• При вдыхании паров или продуктов горения вывести пострадавшего из загрязненной зоны.",
    "• Lay patient down. Keep warm and rested.": "• Уложить пострадавшего. Согреть и обеспечить покой.",
    "• Other measures are usually unnecessary.": "• Дополнительные меры обычно не требуются.",
    "• Immediately give a glass of water.": "• Немедленно дать стакан воды.",
    "• First aid is not generally required. If in doubt, contact a Poisons Information Center or a doctor.": "• Первая помощь обычно не требуется. При сомнениях обратиться в токсикологический центр или к врачу.",
    "• Safety glasses with side shields.": "• Защитные очки с боковыми щитками.",
    "• Immediately remove all contaminated clothing, including footwear": "• Немедленно снять всю загрязненную одежду, включая обувь.",
    "• Flush skin and hair with running water (and soap if available).": "• Промыть кожу и волосы проточной водой (и мылом, если имеется).",
    "continued...": "продолжение...",
}

SIMPLE_TOKEN_MAP = {
    "Flammability": "Воспламеняемость",
    "Toxicity": "Токсичность",
    "Body": "Контакт",
    "Contact": "с телом",
    "Reactivity": "Реактивность",
    "Chronic": "Хронические эффекты",
    "Min/Nil=0": "Мин=0",
    "Low=1": "Низк=1",
    "Moderate=2": "Умер=2",
    "High=3": "Выс=3",
    "Extreme=4": "Чрезв=4",
    "upper": "верхний",
    "lower": "нижний",
    "current": "текущий",
    "Limits": "Пределы",
    "Permissible": "Допустимый",
    "Occupational": "Профессиональный",
    "Exposure": "воздействия",
    "Air": "воздуха",
    "Contaminants": "загрязняющие вещества",
    "Source": "Источник",
    "Material": "Материал",
    "Notes": "Примечания",
}

NORMALIZATION_REPLACEMENTS = {
    "Магазин закрыт": "Хранить под замком",
    "Сохраняйте хладнокровие": "Хранить в прохладном месте",
    "Токсикологический центр": "ТОКСИКОЛОГИЧЕСКИЙ ЦЕНТР",
    "паспорт безопасности": "Паспорт безопасности",
    "Паспорт Данных Безопасности": "Паспорт безопасности",
    "вызывать раздражение органов дыхания": "вызывать раздражение дыхательных путей",
    "при необходимости обратитесь за медицинской помощью": "Обратиться за медицинской помощью",
}

# Стандартные названия разделов GHS/MSDS — переводятся локально, без API
MSDS_SECTION_TITLES: dict[str, str] = {
    "CHEMICAL PRODUCT AND COMPANY IDENTIFICATION": "ИДЕНТИФИКАЦИЯ ХИМИЧЕСКОГО ПРОДУКТА И КОМПАНИИ",
    "PRODUCT AND COMPANY IDENTIFICATION": "ИДЕНТИФИКАЦИЯ ПРОДУКТА И КОМПАНИИ",
    "HAZARDS IDENTIFICATION": "ИДЕНТИФИКАЦИЯ ОПАСНОСТЕЙ",
    "HAZARD IDENTIFICATION": "ИДЕНТИФИКАЦИЯ ОПАСНОСТЕЙ",
    "COMPOSITION / INFORMATION ON INGREDIENTS": "СОСТАВ / ИНФОРМАЦИЯ ОБ ИНГРЕДИЕНТАХ",
    "COMPOSITION/INFORMATION ON INGREDIENTS": "СОСТАВ / ИНФОРМАЦИЯ ОБ ИНГРЕДИЕНТАХ",
    "COMPOSITION / INFORMATION ON INGREDIENT": "СОСТАВ / ИНФОРМАЦИЯ ОБ ИНГРЕДИЕНТАХ",
    "FIRST AID MEASURES": "ПЕРВАЯ ПОМОЩЬ",
    "FIRE FIGHTING MEASURES": "МЕРЫ ПОЖАРОТУШЕНИЯ",
    "FIREFIGHTING MEASURES": "МЕРЫ ПОЖАРОТУШЕНИЯ",
    "FIRE-FIGHTING MEASURES": "МЕРЫ ПОЖАРОТУШЕНИЯ",
    "ACCIDENTAL RELEASE MEASURES": "МЕРЫ ПРИ АВАРИЙНОМ ВЫБРОСЕ",
    "HANDLING AND STORAGE": "ОБРАЩЕНИЕ И ХРАНЕНИЕ",
    "EXPOSURE CONTROLS / PERSONAL PROTECTION": "КОНТРОЛЬ ВОЗДЕЙСТВИЯ / СРЕДСТВА ЗАЩИТЫ",
    "EXPOSURE CONTROLS/PERSONAL PROTECTION": "КОНТРОЛЬ ВОЗДЕЙСТВИЯ / СРЕДСТВА ЗАЩИТЫ",
    "EXPOSURE CONTROLS": "КОНТРОЛЬ ВОЗДЕЙСТВИЯ",
    "PHYSICAL AND CHEMICAL PROPERTIES": "ФИЗИЧЕСКИЕ И ХИМИЧЕСКИЕ СВОЙСТВА",
    "PHYSICAL PROPERTIES": "ФИЗИЧЕСКИЕ СВОЙСТВА",
    "CHEMICAL STABILITY": "ХИМИЧЕСКАЯ СТАБИЛЬНОСТЬ",
    "STABILITY AND REACTIVITY": "СТАБИЛЬНОСТЬ И РЕАКЦИОННАЯ СПОСОБНОСТЬ",
    "TOXICOLOGICAL INFORMATION": "ТОКСИКОЛОГИЧЕСКАЯ ИНФОРМАЦИЯ",
    "ECOLOGICAL INFORMATION": "ЭКОЛОГИЧЕСКАЯ ИНФОРМАЦИЯ",
    "DISPOSAL CONSIDERATIONS": "УТИЛИЗАЦИЯ",
    "CONSIDERACIONES SOBRE LA ELIMINACIÓN": "РЕКОМЕНДАЦИИ ПО УТИЛИЗАЦИИ",
    "TRANSPORTATION INFORMATION": "ИНФОРМАЦИЯ О ТРАНСПОРТИРОВКЕ",
    "TRANSPORT INFORMATION": "ИНФОРМАЦИЯ О ТРАНСПОРТИРОВКЕ",
    "REGULATORY INFORMATION": "НОРМАТИВНАЯ ИНФОРМАЦИЯ",
    "OTHER INFORMATION": "ПРОЧАЯ ИНФОРМАЦИЯ",
}

COMPACT_REPLACEMENTS = (
    ("Название продукта", "Продукт"),
    ("Химическое название", "Хим. название"),
    ("Химическая формула", "Формула"),
    ("Номер детали продукта", "Номер детали"),
    ("Идентифицированные виды применения", "Применение"),
    ("Соответствующие идентифицированные виды применения", "Применение"),
    ("Не применяется", "н/п"),
    ("Не применимо", "н/п"),
    ("Недоступно", "н/д"),
    ("Нет данных", "н/д"),
    ("Источник", "Ист."),
    ("Компонент", "Комп."),
    ("Наименование вещества", "Вещество"),
    ("Наименование материала", "Материал"),
    ("Правильное название для перевозки", "Отгруз. наим."),
    ("Прочие способы идентификации", "Идентификация"),
    ("Категория химического продукта", "Категория"),
    ("Идентифицированные применения", "Применение"),
    ("Применения, которых следует избегать", "Избегать"),
    ("Промышленные применения", "Пром. прим."),
    ("Моющие и чистящие средства", "Моющ./чистящ. ср-ва"),
    ("Производитель/Поставщик", "Произв./Поставщик"),
    ("Ассоциация / Организация", "Ассоц./Орг."),
    ("Экстренный телефон", "Экстр. тел."),
    ("Примечания", "Прим."),
    ("литров", "л"),
    ("литра", "л"),
    ("литр", "л"),
    ("в хорошо проветриваемом месте", "в проветриваемом месте"),
    ("или к врачу/терапевту", "или к врачу"),
    ("Обратиться за медицинской помощью/советом", "Обратиться за медицинской помощью"),
    ("Немедленно позвоните в", "Немедленно обратиться в"),
    ("Немедленно позвонить в", "Немедленно обратиться в"),
    ("долгосрочными последствиями", "длит. последствия"),
    ("дыхательных путей", "дых. путей"),
    ("Предельно допустимые уровни воздействия", "Пределы воздействия"),
    ("Токсично для водной среды, длительное действие", "Токсично для водной среды, длит. действие"),
    ("Может быть смертельно при проглатывании и попадании в дыхательные пути", "Смертельно при проглатывании и аспирации"),
    ("При необходимости обращения за медицинской помощью иметь при себе упаковку продукта или этикетку", "При обращении к врачу иметь тару или этикетку"),
    ("ТОКСИКОЛОГИЧЕСКИЙ ЦЕНТР/к врачу/медику/лицу, оказывающему первую помощь", "токсикологический центр или к врачу"),
    ("ТОКСИКОЛОГИЧЕСКИЙ ЦЕНТР/к врачу/медику", "токсикологический центр или к врачу"),
    ("немедленно обратиться в токсикологический центр или к врачу", "немедленно обратиться в токсикологический центр/к врачу"),
    ("при плохом самочувствии обратиться в токсикологический центр или к врачу", "при плохом самочувствии обратиться в токсикологический центр/к врачу"),
    ("Если врач недоступен более 15 минут, вызвать рвоту (если пострадавший в сознании)", "Если врач недоступен >15 мин, вызвать рвоту (если в сознании)"),
    ("Выстирать загрязненную одежду перед повторным использованием", "Стирать загрязненную одежду перед повторным применением"),
    ("Абсорбировать разлив для предотвращения материального ущерба", "Абсорбировать разлив во избежание ущерба"),
    ("Если раздражение глаз не проходит: обратиться за медицинской консультацией/помощью", "Если раздражение глаз сохраняется: обратиться к врачу"),
    ("Смертельно при проглатывании и аспирации", "Смертельно при проглатывании и аспирации"),
    ("Без ограничения", "Без огранич."),
)

INFORMAL_PATTERNS = (
    r"\bты\b",
    r"\bтебя\b",
    r"\bтебе\b",
    r"\bтвой\b",
    r"\bтвои\b",
)

BAD_MODEL_RESPONSE_RE = re.compile(
    r"please provide|specific text|source text|current russian translation|"
    r"can't provide a translation|cannot provide|no corrections needed|"
    r"no actual text|without the specific text|unable to provide",
    re.IGNORECASE,
)

BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "translation": {"type": "string"},
                    "issues": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "compact": {"type": "string"},
                },
                "required": ["id", "translation", "issues", "compact"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


class BlockKind(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE_CELL = "table_cell"
    NUMERIC_CODE = "numeric_code"


@dataclass
class BlockClassification:
    kind: BlockKind
    reasons: list[str] = field(default_factory=list)


@dataclass
class TranslationArtifact:
    source_text: str
    translated_text: str
    normalized_text: str
    final_text: str
    kind: str
    issues: list[str] = field(default_factory=list)
    fit_candidates: list[str] = field(default_factory=list)
    backend: str = "openai"
    from_cache: bool = False


@dataclass
class TranslationUnit:
    unit_id: str
    text: str
    kind: BlockKind


class MsdsTranslationEngine:
    def __init__(
        self,
        *,
        model: str = "gpt-5.4-mini",
        source_lang: str = "en",
        target_lang: str = "ru",
        api_key: str | None = None,
        cache_path: Path | None = None,
        log_path: Path | None = None,
    ) -> None:
        self.model = model
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.cache_path = cache_path
        self.log_path = log_path
        self.translation_memory: dict[str, str] = {}
        self.cache: dict[str, dict[str, Any]] = self._load_cache(cache_path)
        self.line_memory: dict[str, str] = self._build_line_memory()

        key = api_key or os.environ.get("OPENAI_API_KEY")
        self.client = OpenAI(api_key=key) if key else None

    def classify_block(self, text: str, block: dict | None = None) -> BlockClassification:
        cleaned = self._normalize_whitespace(text)
        reasons: list[str] = []

        if block and self._looks_tabular(block):
            reasons.append("tabular-layout")
            return BlockClassification(BlockKind.TABLE_CELL, reasons)

        if self.is_numeric_or_code(cleaned):
            reasons.append("code-or-numeric")
            return BlockClassification(BlockKind.NUMERIC_CODE, reasons)

        if self._looks_like_heading(cleaned, block):
            reasons.append("heading-shape")
            return BlockClassification(BlockKind.HEADING, reasons)

        reasons.append("default-paragraph")
        return BlockClassification(BlockKind.PARAGRAPH, reasons)

    def translate_many(self, units: list[TranslationUnit]) -> dict[str, TranslationArtifact]:
        results: dict[str, TranslationArtifact] = {}
        pending: list[TranslationUnit] = []

        for unit in units:
            local_hit = self.resolve_local(unit)
            if local_hit is not None:
                results[unit.unit_id] = local_hit
                continue

            pending.append(unit)

        for chunk in self._chunk_units(pending):
            batch_results = self._translate_chunk(chunk)
            results.update(batch_results)

        return results

    def _translate_section_header(self, text: str) -> str | None:
        """
        Локально переводит стандартные заголовки разделов GHS/MSDS.
        Форматы: 'Section N - TITLE', 'SECTION N: TITLE',
        'SECTION N TITLE', 'SECCIÓN N / TITLE' → 'Раздел N – ПЕРЕВОД'
        Возвращает готовый перевод или None если не распознан паттерн.
        """
        m = re.match(
            r"^(?:Section|SECTION|SECCIÓN)\s+(\d+)\s*(?:[:/\\–-]\s*)?(.+)$",
            text.strip(),
            re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return None
        num_part = f"Раздел {m.group(1)}"
        title_part = re.sub(r"\s+", " ", m.group(2).strip().upper())
        ru_title = MSDS_SECTION_TITLES.get(title_part)
        if ru_title:
            return f"{num_part} – {ru_title}"
        # Если конкретный заголовок не найден в словаре — вернём хотя бы локализованный номер
        # Полный текст пойдёт в API для перевода названия
        return None

    def resolve_local(self, unit: TranslationUnit) -> TranslationArtifact | None:
        source_text = unit.text.strip()
        if not source_text:
            return TranslationArtifact(
                source_text="",
                translated_text="",
                normalized_text="",
                final_text="",
                kind=unit.kind.value,
            )

        if unit.kind == BlockKind.NUMERIC_CODE:
            return TranslationArtifact(
                source_text=source_text,
                translated_text=source_text,
                normalized_text=source_text,
                final_text=source_text,
                kind=unit.kind.value,
                backend="identity",
            )

        # Локальный перевод заголовков разделов (Section X - TITLE → Раздел X – ПЕРЕВОД)
        section_hit = self._translate_section_header(source_text)
        if section_hit is not None:
            artifact = TranslationArtifact(
                source_text=source_text,
                translated_text=section_hit,
                normalized_text=section_hit,
                final_text=section_hit,
                kind=unit.kind.value,
                issues=[],
                fit_candidates=self.build_fit_candidates(section_hit, unit.kind),
                backend="section-title-map",
            )
            self._store_artifact(unit, artifact)
            return artifact

        boilerplate_hit = self._translate_boilerplate_block(unit)
        if boilerplate_hit is not None:
            self._store_artifact(unit, boilerplate_hit)
            return boilerplate_hit

        glossary_key = source_text if source_text in SOURCE_GLOSSARY else self._normalize_whitespace(source_text)
        if glossary_key in SOURCE_GLOSSARY:
            translated = SOURCE_GLOSSARY[glossary_key]
            normalized = self.normalize_translation(source_text, translated)
            final_text, issues = self.validate_translation(source_text, normalized, unit.kind)
            artifact = TranslationArtifact(
                source_text=source_text,
                translated_text=translated,
                normalized_text=normalized,
                final_text=final_text,
                kind=unit.kind.value,
                issues=issues,
                fit_candidates=self.build_fit_candidates(final_text, unit.kind),
                backend="glossary",
            )
            self._store_artifact(unit, artifact)
            return artifact

        simple_hit = self._translate_with_simple_rules(unit)
        if simple_hit is not None:
            self._store_artifact(unit, simple_hit)
            return simple_hit

        if re.fullmatch(r"[A-Z][A-Z0-9®™+./-]{2,}", source_text):
            artifact = TranslationArtifact(
                source_text=source_text,
                translated_text=source_text,
                normalized_text=source_text,
                final_text=source_text,
                kind=unit.kind.value,
                issues=[],
                fit_candidates=self.build_fit_candidates(source_text, unit.kind),
                backend="product-identifier",
            )
            self._store_artifact(unit, artifact)
            return artifact

        memory_hit = self.translation_memory.get(source_text)
        if memory_hit:
            return TranslationArtifact(
                source_text=source_text,
                translated_text=memory_hit,
                normalized_text=memory_hit,
                final_text=memory_hit,
                kind=unit.kind.value,
                backend="memory",
                from_cache=True,
                fit_candidates=self.build_fit_candidates(memory_hit, unit.kind),
            )

        cache_key = f"{unit.kind.value}::{source_text}"
        cached = self.cache.get(cache_key)
        if cached:
            final_text = str(cached.get("final_text", source_text))
            if self._is_bad_model_response(final_text):
                self.log_event("cache_skip_bad_response", source_preview=source_text[:120], final_preview=final_text[:120])
            else:
                final_text = self.normalize_translation(source_text, final_text)
                self.translation_memory[source_text] = final_text
                return TranslationArtifact(
                    source_text=source_text,
                    translated_text=str(cached.get("translated_text", final_text)),
                    normalized_text=str(cached.get("normalized_text", final_text)),
                    final_text=final_text,
                    kind=unit.kind.value,
                    issues=list(cached.get("issues", [])),
                    fit_candidates=self.build_fit_candidates(final_text, unit.kind),
                    from_cache=True,
                )

        line_memory_hit = self._translate_with_line_memory(unit)
        if line_memory_hit is not None:
            self._store_artifact(unit, line_memory_hit)
            return line_memory_hit

        return None

    def normalize_translation(self, source_text: str, translated_text: str) -> str:
        text = self._strip_pdf_artifacts(translated_text).strip()
        if source_text in SOURCE_GLOSSARY:
            text = SOURCE_GLOSSARY[source_text]

        for old, new in NORMALIZATION_REPLACEMENTS.items():
            text = text.replace(old, new)

        text = re.sub(r"\bРаздел\s*([0-9]+)\s*[-:]\s*", r"Раздел \1 – ", text)
        text = re.sub(r"\s+([,.;:])", r"\1", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def validate_translation(self, source_text: str, translated_text: str, kind: BlockKind) -> tuple[str, list[str]]:
        issues: list[str] = []
        final_text = translated_text

        if source_text == translated_text and not self.is_numeric_or_code(source_text):
            issues.append("untranslated-text")

        if self._is_bad_model_response(translated_text):
            issues.append("bad-model-response")

        if any(re.search(pattern, translated_text, flags=re.IGNORECASE) for pattern in INFORMAL_PATTERNS):
            issues.append("informal-language")

        if "patient" in source_text.lower() and "пациент" not in translated_text.lower():
            if re.search(r"\bвы\b|\bвас\b|\bвам\b", translated_text.lower()):
                issues.append("wrong-subject-reference")

        for pattern in PROTECTED_TOKEN_PATTERNS:
            source_tokens = re.findall(pattern, source_text)
            target_tokens = re.findall(pattern, translated_text)
            if source_tokens and source_tokens != target_tokens:
                issues.append("protected-token-mismatch")
                break

        if issues:
            final_text = self._repair_translation(source_text, translated_text, kind, issues)
            final_text = self.normalize_translation(source_text, final_text)

        return final_text, issues

    def build_fit_candidates(self, text: str, kind: BlockKind) -> list[str]:
        candidates = [text.strip()]
        compact = candidates[0]
        for old, new in COMPACT_REPLACEMENTS:
            compact = compact.replace(old, new)
        if kind == BlockKind.HEADING:
            compact = compact.replace(" / ", "/").replace(" – ", " - ")
        compact = re.sub(r"[ \t]+", " ", compact).strip()
        if compact and compact not in candidates:
            candidates.append(compact)

        inline_variants = self._build_inline_fit_candidates(candidates[0])
        for variant in inline_variants:
            compact_variant = variant
            for old, new in COMPACT_REPLACEMENTS:
                compact_variant = compact_variant.replace(old, new)
            compact_variant = re.sub(r"[ \t]+", " ", compact_variant).strip()
            for item in (variant, compact_variant):
                if item and item not in candidates:
                    candidates.append(item)
        return candidates

    def _build_inline_fit_candidates(self, text: str) -> list[str]:
        """
        Dense SDS tables often extract one visual row as several newline-separated
        spans, while the PDF box is only one text line high. Try single-line
        variants before the renderer gives up and leaves English in place.
        """
        lines = [line.strip(" \t:") for line in text.splitlines() if line.strip()]
        if len(lines) < 2 or len(lines) > 8:
            return []

        variants: list[str] = []
        joined_rest = " / ".join(lines[1:])
        if joined_rest:
            variants.append(f"{lines[0]}: {joined_rest}")
            variants.append(joined_rest)

        single_line = " / ".join(lines)
        if single_line:
            variants.append(single_line)

        return variants

    def log_event(self, event_type: str, **payload: Any) -> None:
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": event_type, **payload}, ensure_ascii=False) + "\n")

    def is_numeric_or_code(self, text: str) -> bool:
        cleaned = self._normalize_whitespace(text)
        if not cleaned:
            return True
        if not re.search(r"[A-Za-zА-Яа-я0-9]", cleaned):
            return True
        if re.fullmatch(r"[•*+\-–—oOxXvB\s]+", cleaned):
            return True
        ghs_code = r"(?:H\d{3}|EUH\d+|P\d{3}(?:\+P\d{3})*)"
        if re.fullmatch(rf"{ghs_code}(?:\s*[,;/]\s*{ghs_code})*", cleaned):
            return True
        if re.fullmatch(r"\d{2,7}-\d{2}-\d", cleaned):
            return True
        if re.fullmatch(r"[\d .,%<>()/\-:]+", cleaned):
            return True
        tokens = re.findall(r"[A-Za-z0-9]+", cleaned)
        if tokens and all(token.isupper() for token in tokens) and any(any(ch.isdigit() for ch in token) for token in tokens):
            return True
        return False

    def _translate_chunk(self, units: list[TranslationUnit]) -> dict[str, TranslationArtifact]:
        try:
            raw = self._call_llm_structured(units)
        except Exception as exc:
            self.log_event(
                "chunk_fallback",
                error=repr(exc),
                unit_count=len(units),
                preview=units[0].text[:160] if units else "",
            )
            if len(units) > 1:
                mid = len(units) // 2
                left = self._translate_chunk(units[:mid])
                right = self._translate_chunk(units[mid:])
                return {**left, **right}
            return {units[0].unit_id: self._translate_single_unit(units[0], exc)}
        returned = {item["id"]: item for item in raw.get("items", []) if item.get("id")}
        results: dict[str, TranslationArtifact] = {}

        for unit in units:
            source_text = unit.text.strip()
            item = returned.get(unit.unit_id)
            translated = (item or {}).get("translation", source_text)
            normalized = self.normalize_translation(source_text, translated)
            final_text, issues = self.validate_translation(source_text, normalized, unit.kind)
            issues = list(dict.fromkeys(list((item or {}).get("issues", [])) + issues))

            artifact = TranslationArtifact(
                source_text=source_text,
                translated_text=translated,
                normalized_text=normalized,
                final_text=final_text,
                kind=unit.kind.value,
                issues=issues,
                fit_candidates=self._fit_candidates_from_item(final_text, unit.kind, item),
            )
            self._store_artifact(unit, artifact)
            self.log_event(
                "translation",
                kind=unit.kind.value,
                from_cache=False,
                issues=issues,
                unit_id=unit.unit_id,
                source_preview=source_text[:200],
                final_preview=final_text[:200],
            )
            results[unit.unit_id] = artifact

        return results

    def _translate_single_unit(self, unit: TranslationUnit, original_error: Exception | None = None) -> TranslationArtifact:
        source_text = unit.text.strip()
        translated = source_text
        issues: list[str] = []

        if self.client is not None:
            prompt = (
                "Translate this single GHS/MSDS PDF unit from English to Russian.\n"
                f"Kind: {unit.kind.value}\n"
                "Rules:\n"
                "- preserve meaning exactly\n"
                "- use official Russian chemical safety terminology\n"
                "- keep wording compact enough for the original PDF block\n"
                "- preserve section numbers, hazard codes, CAS numbers, percentages, units, formulas, product identifiers\n"
                "- return only the translated text\n\n"
                f"Text:\n{source_text}"
            )
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    response = self.client.responses.create(
                        model=self.model,
                        instructions=SYSTEM_PROMPT,
                        input=prompt,
                        max_output_tokens=1200,
                        timeout=120,
                    )
                    translated = (response.output_text or source_text).strip()
                    break
                except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
                    last_error = exc
                    self.log_event(
                        "llm_retry",
                        stage="single_unit_fallback",
                        attempt=attempt + 1,
                        error=repr(exc),
                        kind=unit.kind.value,
                        source_preview=source_text[:120],
                    )
                    time.sleep(min(10, 2 * (attempt + 1)))
            if last_error is not None and translated == source_text:
                issues.append("fallback-identity")

        normalized = self.normalize_translation(source_text, translated)
        final_text, validation_issues = self.validate_translation(source_text, normalized, unit.kind)
        issues = list(dict.fromkeys(([f"chunk-fallback:{type(original_error).__name__}"] if original_error else []) + issues + validation_issues))
        artifact = TranslationArtifact(
            source_text=source_text,
            translated_text=translated,
            normalized_text=normalized,
            final_text=final_text,
            kind=unit.kind.value,
            issues=issues,
            fit_candidates=self.build_fit_candidates(final_text, unit.kind),
            backend="single-unit-fallback",
        )
        self._store_artifact(unit, artifact)
        self.log_event(
            "translation",
            kind=unit.kind.value,
            from_cache=False,
            issues=issues,
            unit_id=unit.unit_id,
            source_preview=source_text[:200],
            final_preview=final_text[:200],
        )
        return artifact

    def _fit_candidates_from_item(self, final_text: str, kind: BlockKind, item: dict[str, Any] | None) -> list[str]:
        base = self.build_fit_candidates(final_text, kind)
        compact = ""
        if item:
            compact = str(item.get("compact", "")).strip()
        if compact and compact not in base:
            return [base[0], compact, *base[1:]]
        return base

    def _store_artifact(self, unit: TranslationUnit, artifact: TranslationArtifact) -> None:
        source = unit.text.strip()
        final = artifact.final_text.strip()
        if self._is_bad_model_response(final):
            self.log_event("cache_skip_bad_response", source_preview=source[:120], final_preview=final[:120])
            return
        # Do NOT cache identity translations of translatable content —
        # they indicate an API failure and would poison future runs.
        is_identity = (final == source or not final)
        has_translatable = bool(re.search(r"[A-Za-z]{3,}", source))
        is_numeric = self.is_numeric_or_code(source)
        fallback_issue = any(
            issue == "fallback-identity" or issue == "untranslated-text" or issue.startswith("chunk-fallback:")
            for issue in artifact.issues
        )
        if is_identity and has_translatable and not is_numeric and fallback_issue:
            self.log_event("cache_skip_identity", source_preview=source[:120])
            return
        self.translation_memory[source] = final
        self.cache[f"{unit.kind.value}::{source}"] = asdict(artifact)
        self._learn_line_memory(source, final)
        self._save_cache()

    def _is_bad_model_response(self, text: str) -> bool:
        return bool(BAD_MODEL_RESPONSE_RE.search(text or ""))

    def _line_key(self, text: str) -> str:
        return self._normalize_whitespace(text).strip()

    def _build_line_memory(self) -> dict[str, str]:
        memory: dict[str, str] = {}

        for source, target in SOURCE_GLOSSARY.items():
            memory[self._line_key(source)] = target.strip()

        for payload in self.cache.values():
            source_text = str(payload.get("source_text", "")).strip()
            final_text = str(payload.get("final_text", "")).strip()
            if self._is_bad_model_response(final_text):
                continue
            self._learn_line_memory(source_text, final_text, target=memory)

        return memory

    def _learn_line_memory(self, source_text: str, final_text: str, target: dict[str, str] | None = None) -> None:
        target = target or self.line_memory
        source_lines = [line.strip() for line in source_text.splitlines() if line.strip()]
        final_lines = [line.strip() for line in final_text.splitlines() if line.strip()]
        if not source_lines or len(source_lines) != len(final_lines):
            return

        for src, dst in zip(source_lines, final_lines):
            if len(src) > 140 or len(dst) > 180:
                continue
            if self.is_numeric_or_code(src) and src == dst:
                continue
            if src == dst and re.search(r"[A-Za-z]{3,}", src):
                continue
            if not dst:
                continue
            target[self._line_key(src)] = dst

    def _translate_with_line_memory(self, unit: TranslationUnit) -> TranslationArtifact | None:
        source_text = unit.text.strip()
        source_lines = [line.strip() for line in source_text.splitlines() if line.strip()]
        if len(source_lines) < 2:
            return None

        translated_lines: list[str] = []
        for line in source_lines:
            key = self._line_key(line)
            if line in SOURCE_GLOSSARY:
                translated_lines.append(SOURCE_GLOSSARY[line])
                continue
            if self.is_numeric_or_code(line):
                translated_lines.append(line)
                continue
            hit = self.line_memory.get(key)
            if hit:
                translated_lines.append(hit)
                continue
            return None

        translated = "\n".join(translated_lines).strip()
        normalized = self.normalize_translation(source_text, translated)
        final_text, issues = self.validate_translation(source_text, normalized, unit.kind)
        return TranslationArtifact(
            source_text=source_text,
            translated_text=translated,
            normalized_text=normalized,
            final_text=final_text,
            kind=unit.kind.value,
            issues=issues,
            fit_candidates=self.build_fit_candidates(final_text, unit.kind),
            backend="line-memory",
            from_cache=True,
        )

    def _translate_with_simple_rules(self, unit: TranslationUnit) -> TranslationArtifact | None:
        source_text = unit.text.strip()
        source_lines = [line.strip() for line in source_text.splitlines() if line.strip()]
        if not source_lines or len(source_lines) > 12:
            return None

        translated_lines: list[str] = []
        for line in source_lines:
            translated_line = self._translate_simple_line(line)
            if translated_line is None:
                return None
            translated_lines.append(translated_line)

        translated = "\n".join(translated_lines).strip()
        normalized = self.normalize_translation(source_text, translated)
        final_text, issues = self.validate_translation(source_text, normalized, unit.kind)
        return TranslationArtifact(
            source_text=source_text,
            translated_text=translated,
            normalized_text=normalized,
            final_text=final_text,
            kind=unit.kind.value,
            issues=issues,
            fit_candidates=self.build_fit_candidates(final_text, unit.kind),
            backend="simple-rules",
            from_cache=True,
        )

    def _translate_simple_line(self, line: str) -> str | None:
        stripped = self._normalize_whitespace(line.strip())
        if not stripped:
            return ""
        if self.is_numeric_or_code(stripped):
            return stripped
        if stripped in SOURCE_GLOSSARY:
            return SOURCE_GLOSSARY[stripped]
        if stripped in SIMPLE_LINE_MAP:
            return SIMPLE_LINE_MAP[stripped]
        numbered = re.fullmatch(r"(\d{1,2}\.\d+)\s+(.+)", stripped)
        if numbered:
            prefix, rest = numbered.groups()
            translated_rest = self._translate_simple_line(rest)
            if translated_rest:
                return f"{prefix} {translated_rest}"
        code_statement = re.fullmatch(r"((?:H\d{3}|EUH\d+|P\d{3}(?:\+P\d{3})*)+)\s+(.+)", stripped)
        if code_statement:
            code, statement = code_statement.groups()
            translated_statement = SOURCE_GLOSSARY.get(statement)
            if translated_statement is None and statement.endswith("."):
                translated_statement = SOURCE_GLOSSARY.get(statement[:-1])
                if translated_statement and not translated_statement.endswith("."):
                    translated_statement += "."
            if translated_statement:
                return f"{code} {translated_statement}"
        line_memory_hit = self.line_memory.get(self._line_key(stripped))
        if line_memory_hit:
            return line_memory_hit
        if len(stripped) > 80:
            return None

        tokens = re.findall(r"[A-Za-z][A-Za-z/&.-]*=?\d*|\([^)]+\)|[%°/]+|[0-9]+|[^\sA-Za-z0-9]", stripped)
        out: list[str] = []
        for token in tokens:
            if token.isspace():
                out.append(token)
                continue
            if re.fullmatch(r"[0-9]+|[%°/]+|\([^)]+\)|[^\w\s]", token):
                out.append(token)
                continue
            mapped = SIMPLE_TOKEN_MAP.get(token)
            if mapped is None:
                return None
            out.append(mapped)

        translated = " ".join(part for part in out if part)
        translated = translated.replace(" (", "(").replace(" )", ")").replace(" / ", "/")
        translated = re.sub(r"\s+([,.;:])", r"\1", translated)
        translated = re.sub(r"[ \t]+", " ", translated).strip()
        return translated or None

    def _translate_boilerplate_block(self, unit: TranslationUnit) -> TranslationArtifact | None:
        source_text = unit.text.strip()
        lines = [line.strip() for line in source_text.splitlines() if line.strip()]
        if not lines:
            return None

        translated_lines: list[str] = []
        used_template = False

        for line in lines:
            translated_line = self._translate_boilerplate_line(line)
            if translated_line is None:
                return None
            if translated_line != line:
                used_template = True
            translated_lines.append(translated_line)

        if not used_template:
            return None

        translated = "\n".join(translated_lines).strip()
        normalized = self.normalize_translation(source_text, translated)
        final_text, issues = self.validate_translation(source_text, normalized, unit.kind)
        return TranslationArtifact(
            source_text=source_text,
            translated_text=translated,
            normalized_text=normalized,
            final_text=final_text,
            kind=unit.kind.value,
            issues=issues,
            fit_candidates=self.build_fit_candidates(final_text, unit.kind),
            backend="boilerplate",
            from_cache=True,
        )

    def _translate_boilerplate_line(self, line: str) -> str | None:
        stripped = line.strip()
        if not stripped:
            return ""
        if stripped in SOURCE_GLOSSARY:
            return SOURCE_GLOSSARY[stripped]
        if self.is_numeric_or_code(stripped):
            return stripped

        direct_map = {
            "Chemwatch GHS Safety Data Sheet": "Паспорт безопасности GHS Chemwatch",
            "GHS Classification": "Классификация по GHS",
            "Issue Date:": "Дата выпуска:",
            "Print Date:": "Дата печати:",
        }
        if stripped in direct_map:
            return direct_map[stripped]

        if re.match(r"^Section\s+\d+\s*[-–]", stripped, flags=re.IGNORECASE):
            left, right = re.split(r"\s*[-–]\s*", stripped, maxsplit=1)
            right_key = right.strip()
            translated_right = self.line_memory.get(self._line_key(right_key), right_key)
            return f"{left.replace('Section', 'Раздел')} – {translated_right}"

        if stripped.startswith("Issue Date:"):
            return "Дата выпуска:" + stripped[len("Issue Date:") :]
        if stripped.startswith("Initial Date:"):
            return "Нач. дата:" + stripped[len("Initial Date:") :]
        if stripped.startswith("Revision Date:"):
            return "Ред.:" + stripped[len("Revision Date:") :]
        if stripped.startswith("Print Date:"):
            return "Дата печати:" + stripped[len("Print Date:") :]

        if re.match(r"^CD\s+\d{4}/\d+\s+Page\s+\d+\s+of\s+\d+$", stripped):
            return re.sub(r"\bPage\s+(\d+)\s+of\s+(\d+)\b", r"Стр. \1 из \2", stripped)

        if re.match(r"^Version No:\s*", stripped):
            return stripped.replace("Version No:", "Версия №:")

        if stripped.startswith("CHEMWATCH "):
            return stripped

        if stripped.endswith("GHS Safety Data Sheet"):
            prefix = stripped[: -len("GHS Safety Data Sheet")].strip()
            if prefix:
                return f"{prefix} Паспорт безопасности GHS"

        return None

    def _chunk_units(self, units: list[TranslationUnit], *, max_items: int = 6, max_chars: int = 2000) -> list[list[TranslationUnit]]:
        chunks: list[list[TranslationUnit]] = []
        current: list[TranslationUnit] = []
        current_chars = 0
        for unit in units:
            unit_chars = len(unit.text)
            if current and (len(current) >= max_items or current_chars + unit_chars > max_chars):
                chunks.append(current)
                current = []
                current_chars = 0
            current.append(unit)
            current_chars += unit_chars
        if current:
            chunks.append(current)
        return chunks

    def _call_llm_structured(self, units: list[TranslationUnit]) -> dict[str, Any]:
        if self.client is None:
            raise RuntimeError("OPENAI_API_KEY is required for msds_translation_engine")

        payload = [
            {"id": unit.unit_id, "kind": unit.kind.value, "text": unit.text}
            for unit in units
        ]
        prompt = (
            "Translate the following GHS/MSDS PDF units from English to Russian.\n\n"
            "Rules by kind:\n"
            "- heading: short compact regulatory heading\n"
            "- paragraph: full technical translation\n"
            "- table_cell: compact technical translation for narrow cells\n"
            "- numeric_code: should be returned unchanged, but these units are normally excluded\n\n"
            "Global rules:\n"
            "- preserve meaning exactly\n"
            "- never simplify safety instructions\n"
            "- preserve section numbers, hazard codes, CAS numbers, percentages, units, formulas, product identifiers\n"
            "- use official Russian chemical safety terminology\n"
            "- return JSON only\n\n"
            f"Preferred standard terms when applicable:\n{json.dumps(SOURCE_GLOSSARY, ensure_ascii=False)}\n\n"
            f"Units:\n{json.dumps(payload, ensure_ascii=False)}"
        )

        max_output = min(5000, max(1200, int(sum(len(unit.text) for unit in units) / 2)))
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    max_completion_tokens=max_output,
                    timeout=180,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "msds_translation_batch",
                            "schema": BATCH_SCHEMA,
                            "strict": True,
                        },
                    },
                )
                content = (response.choices[0].message.content or "").strip()
                try:
                    return json.loads(content)
                except json.JSONDecodeError as exc:
                    last_error = exc
                    if attempt == 3:
                        start = content.find("{")
                        end = content.rfind("}")
                        if start != -1 and end != -1 and end > start:
                            return json.loads(content[start : end + 1])
                        raise
                    max_output = min(7000, int(max_output * 1.35))
            except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
                last_error = exc
                self.log_event(
                    "llm_retry",
                    stage="translate_chunk",
                    attempt=attempt + 1,
                    error=repr(exc),
                    unit_count=len(units),
                )
                time.sleep(min(12, 2 * (attempt + 1)))
        if last_error is not None:
            raise last_error
        raise RuntimeError("unreachable")

    def _repair_translation(self, source_text: str, translated_text: str, kind: BlockKind, issues: list[str]) -> str:
        deterministic = translated_text
        deterministic = deterministic.replace("Магазин закрыт", "Хранить под замком")
        deterministic = deterministic.replace("Сохраняйте хладнокровие", "Хранить в прохладном месте")
        deterministic = re.sub(r"\bТоксикологический центр\b", "ТОКСИКОЛОГИЧЕСКИЙ ЦЕНТР", deterministic)

        if self.client is None:
            return deterministic

        prompt = (
            f"{VALIDATION_PROMPT}\n\n"
            f"Block kind: {kind.value}\n"
            f"Issues: {', '.join(issues)}\n\n"
            f"Source text:\n{source_text}\n\n"
            f"Current Russian translation:\n{deterministic}"
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.client.responses.create(
                    model=self.model,
                    instructions=SYSTEM_PROMPT,
                    input=prompt,
                    max_output_tokens=1200,
                    timeout=120,
                )
                return (response.output_text or deterministic).strip()
            except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
                last_error = exc
                self.log_event(
                    "llm_retry",
                    stage="repair_translation",
                    attempt=attempt + 1,
                    error=repr(exc),
                    kind=kind.value,
                    source_preview=source_text[:120],
                )
                time.sleep(min(10, 2 * (attempt + 1)))
        if last_error is not None:
            self.log_event(
                "repair_fallback",
                error=repr(last_error),
                kind=kind.value,
                source_preview=source_text[:120],
            )
        return deterministic

    def _looks_like_heading(self, text: str, block: dict | None) -> bool:
        if not text:
            return False
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 3:
            return False
        if re.match(r"^Section\s+\d+", text, flags=re.IGNORECASE):
            return True
        upper_ratio = self._uppercase_ratio(text)
        if len(text) <= 120 and upper_ratio >= 0.45:
            return True
        if block:
            spans = [
                span
                for line in block.get("lines", [])
                for span in line.get("spans", [])
                if span.get("text", "").strip()
            ]
            if spans:
                font_names = " ".join(str(span.get("font", "")) for span in spans).lower()
                avg_font = sum(float(span.get("size", 10.0)) for span in spans) / len(spans)
                if "bold" in font_names and len(text) <= 140:
                    return True
                if avg_font >= 9.5 and len(lines) <= 2 and len(text) <= 140:
                    return True
        return False

    def _looks_tabular(self, block: dict) -> bool:
        lines = block.get("lines", [])
        if len(lines) < 3:
            return False
        x_counts: dict[float, int] = {}
        for line in lines:
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            # Allow 1–2 spans per line (mixed bold/normal cells count as 2 spans)
            if len(spans) == 0 or len(spans) > 2:
                return False
            key = round(spans[0]["bbox"][0], 1)
            x_counts[key] = x_counts.get(key, 0) + 1
        # For a block to be tabular: ≥2 column x-positions, each repeated ≥2 times
        repeated_columns = [x for x, count in x_counts.items() if count >= 2]
        return len(repeated_columns) >= 2

    def _uppercase_ratio(self, text: str) -> float:
        letters = [char for char in text if char.isalpha()]
        if not letters:
            return 0.0
        return len([char for char in letters if char.isupper()]) / len(letters)

    def _normalize_whitespace(self, text: str) -> str:
        text = self._strip_pdf_artifacts(text)
        text = text.replace("\u00a0", " ")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return re.sub(r"[ \t]+", " ", text).strip()

    def _strip_pdf_artifacts(self, text: str) -> str:
        # Убрать HTML-теги (могут появиться из PDF или от API)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)  # <br> → перенос
        text = re.sub(r"<[A-Za-z][^>]{0,40}>", "", text)              # остальные теги
        # Убрать PDF escape-последовательности вида <\tag>
        text = re.sub(r"<\\[A-Za-zА-Яа-я]+>", "", text)
        text = re.sub(r"<\\[^>]*>", "", text)
        # Убрать строки состоящие только из точки
        text = re.sub(r"\n?\s*\.\s*\n", "\n", text)
        return text

    def _load_cache(self, cache_path: Path | None) -> dict[str, dict[str, Any]]:
        if not cache_path or not cache_path.exists():
            return {}
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8")
