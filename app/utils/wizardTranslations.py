"""Localized labels for GET /wizard/questions?lang=. Only labels are
translated: question ids and option values stay English, because the submit
handler and the Agro event map those values.

DRAFT translations (Hindi, Marathi) written for the 2026-10-17 gap fixes;
they need a native-speaker review before release. Languages not listed here
fall back to the English labels in routers/v2/wizard.py.
"""

QUESTION_LABELS: dict[str, dict[str, str]] = {
    "hi": {
        "location": "आपका खेत कहाँ है?",
        "season": "आप किस मौसम में बुवाई की योजना बना रहे हैं?",
        "has_active_crop": "क्या अभी आपके खेत में कोई फसल लगी है?",
        "crop": "कौन सी फसल लगी है?",
        "sowing_date": "आपने बुवाई कब की?",
        "previous_crop": "पहले यहाँ कौन सी फसल उगाई थी?",
        "irrigation": "आप इस खेत की सिंचाई कैसे करते हैं?",
        "budget": "अगले मौसम में आप प्रति एकड़ कितना खर्च कर सकते हैं?",
        "livestock": "आपके पास किस प्रकार का फार्म है?",
        "farm_name": "अपने खेत को एक नाम दें",
    },
    "mr": {
        "location": "तुमचे शेत कुठे आहे?",
        "season": "तुम्ही कोणत्या हंगामात पेरणीचे नियोजन करत आहात?",
        "has_active_crop": "सध्या तुमच्या शेतात पीक उभे आहे का?",
        "crop": "कोणते पीक उभे आहे?",
        "sowing_date": "तुम्ही पेरणी कधी केली?",
        "previous_crop": "आधी येथे कोणते पीक घेतले होते?",
        "irrigation": "तुम्ही या शेताला पाणी कसे देता?",
        "budget": "पुढील हंगामात तुम्ही प्रति एकर किती खर्च करू शकता?",
        "livestock": "तुमच्याकडे कोणत्या प्रकारचे फार्म आहे?",
        "farm_name": "तुमच्या शेताला नाव द्या",
    },
}

# question id -> option value -> label. Budget options are rupee ranges and
# stay as they are in every language.
OPTION_LABELS: dict[str, dict[str, dict[str, str]]] = {
    "hi": {
        "season": {
            "Kharif": "खरीफ (मानसून: जून - अक्टूबर)",
            "Rabi": "रबी (सर्दी: अक्टूबर - मार्च)",
            "Summer": "गर्मी / ज़ायद (मार्च - जून)",
            "Annual": "दीर्घकालिक (बहुवर्षीय / वार्षिक)",
        },
        "has_active_crop": {"Yes": "हाँ", "No": "नहीं"},
        "irrigation": {
            "Rainfed": "वर्षा-आधारित",
            "Flood": "बाढ़ / नहर / नाली सिंचाई",
            "Drip": "ड्रिप",
            "Sprinkler": "स्प्रिंकलर",
        },
        "livestock": {
            "Cattle farm - Cows & Buffaloes": "पशु फार्म - गाय और भैंस",
            "Cattle farm - Goat, Sheep, & Pig": "पशु फार्म - बकरी, भेड़ और सूअर",
            "Poultry farm": "मुर्गी पालन फार्म",
            "Fisheries": "मत्स्य पालन",
            "I don't have any live stock": "मेरे पास कोई पशुधन नहीं है",
        },
    },
    "mr": {
        "season": {
            "Kharif": "खरीप (पावसाळा: जून - ऑक्टोबर)",
            "Rabi": "रब्बी (हिवाळा: ऑक्टोबर - मार्च)",
            "Summer": "उन्हाळी / जायद (मार्च - जून)",
            "Annual": "दीर्घकालीन (बहुवार्षिक / वार्षिक)",
        },
        "has_active_crop": {"Yes": "हो", "No": "नाही"},
        "irrigation": {
            "Rainfed": "पावसावर आधारित",
            "Flood": "पाट / कालवा / सरी पद्धत",
            "Drip": "ठिबक",
            "Sprinkler": "तुषार",
        },
        "livestock": {
            "Cattle farm - Cows & Buffaloes": "गुरांचा फार्म - गाई व म्हशी",
            "Cattle farm - Goat, Sheep, & Pig": "पशु फार्म - शेळी, मेंढी व डुक्कर",
            "Poultry farm": "कुक्कुटपालन फार्म",
            "Fisheries": "मत्स्यपालन",
            "I don't have any live stock": "माझ्याकडे पशुधन नाही",
        },
    },
}

__all__ = ["QUESTION_LABELS", "OPTION_LABELS"]
