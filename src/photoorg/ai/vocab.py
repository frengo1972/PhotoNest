"""Automatic tags: Italian label -> English prompts for CLIP (it was trained on English captions).

Grouped by what they help with. The "pulizia" group finds what usually does not belong in a family
archive (screenshots, receipts, memes), so it can be reviewed and excluded in one go.
Changing this list only needs the tags to be recomputed from the stored embeddings, not a new scan.
"""

VOCAB_VERSION = 1

TAGS: dict[str, dict[str, list[str]]] = {
    "luoghi": {
        "mare": ["a photo of the sea", "a photo of the ocean and waves"],
        "spiaggia": ["a photo of a beach", "people on a sandy beach"],
        "montagna": ["a photo of mountains", "a mountain landscape"],
        "neve": ["a photo of snow", "a snowy landscape"],
        "lago": ["a photo of a lake"],
        "fiume": ["a photo of a river"],
        "bosco": ["a photo of a forest", "trees in the woods"],
        "campagna": ["a photo of the countryside", "fields and farmland"],
        "città": ["a photo of a city street", "buildings in a city"],
        "centro storico": ["an old town with historic buildings", "a medieval village street"],
        "monumento": ["a photo of a famous monument", "a historic landmark"],
        "chiesa": ["a photo of a church", "the interior of a church"],
        "museo": ["a photo inside a museum", "paintings in an art gallery"],
        "parco": ["a photo of a park", "a playground in a park"],
        "piscina": ["a photo of a swimming pool"],
        "casa": ["a photo inside a home", "a living room"],
        "cucina": ["a photo of a kitchen"],
        "ristorante": ["a photo in a restaurant", "people eating at a restaurant table"],
        "aeroporto": ["a photo of an airport", "an airplane"],
        "auto": ["a photo of a car"],
        "barca": ["a photo of a boat", "a sailing boat"],
        "treno": ["a photo of a train", "a train station"],
    },
    "momenti": {
        "tramonto": ["a photo of a sunset", "a sunrise over the horizon"],
        "notte": ["a photo taken at night", "city lights at night"],
        "selfie": ["a selfie", "a close-up selfie of a person"],
        "foto di gruppo": ["a group photo of people posing", "a family portrait"],
        "bambini": ["a photo of children playing", "a photo of a child"],
        "neonato": ["a photo of a newborn baby", "a baby"],
        "compleanno": ["a birthday party", "a birthday cake with candles"],
        "matrimonio": ["a wedding", "a bride and groom"],
        "festa": ["a party with friends", "a celebration"],
        "natale": ["a christmas tree", "christmas decorations and presents"],
        "concerto": ["a concert", "a band playing on stage"],
        "scuola": ["a school classroom", "a school event"],
        "laurea": ["a graduation ceremony", "a graduate with a laurel wreath"],
        "vacanza": ["a holiday trip", "tourists on vacation"],
    },
    "attività": {
        "sport": ["people playing sports", "a sports match"],
        "calcio": ["a football match", "children playing soccer"],
        "sci": ["people skiing", "a ski slope"],
        "surf e kite": ["a kitesurfer on the sea", "a surfer riding a wave", "a kite surfing"],
        "nuoto": ["people swimming", "a swimmer in the water"],
        "bicicletta": ["a person riding a bicycle", "a bicycle"],
        "escursione": ["people hiking on a trail", "a hike in the mountains"],
        "danza": ["people dancing", "a dance performance"],
    },
    "cose": {
        "cane": ["a photo of a dog"],
        "gatto": ["a photo of a cat"],
        "cavallo": ["a photo of a horse"],
        "uccelli": ["a photo of birds"],
        "cibo": ["a photo of food on a plate", "a meal"],
        "pizza": ["a photo of pizza"],
        "dolci": ["a photo of a dessert", "cakes and pastries"],
        "fiori": ["a photo of flowers"],
        "disegno": ["a child's drawing", "a painting or artwork"],
    },
    "pulizia": {
        "screenshot": ["a screenshot of a phone screen", "a screenshot of an app", "a screenshot of a chat conversation"],
        "documento": ["a photo of a document", "a scanned paper document with text", "an identity card"],
        "scontrino": ["a photo of a receipt", "an invoice"],
        "testo": ["a photo of text", "a page of a book"],
        "meme": ["a meme with text", "a funny internet meme"],
        "codice qr": ["a QR code", "a barcode"],
        "biglietto": ["a ticket", "a boarding pass"],
    },
}

# Generic descriptions that absorb probability mass from images matching no specific tag.
BACKGROUND = ["a photo", "a picture of something", "a blurry photo", "an image"]

ALL_TAGS: dict[str, list[str]] = {label: prompts for group in TAGS.values() for label, prompts in group.items()}
GROUP_OF: dict[str, str] = {label: group for group, labels in TAGS.items() for label in labels}
