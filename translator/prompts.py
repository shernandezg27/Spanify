"""
Prompts externalizados como constantes. Ninguna otra parte del código
debe contener cadenas de prompt embebidas.

- EPUB_TRANSLATE: prompt para HTML/XHTML de EPUB.
- PDF_TRANSLATE:  prompt para fragmentos de texto plano separados por |||.
- PREV_CONTEXT:   bloque de contexto previo, opcionalmente prepended al prompt.
"""

PREV_CONTEXT = (
    "FRAGMENTO ANTERIOR YA TRADUCIDO (úsalo solo como referencia de tono, "
    "estilo y vocabulario para mantener la coherencia; no lo repitas ni lo traduzcas):\n"
    "{prev}\n\n"
    "---\n\n"
)


EPUB_TRANSLATE = (
    "Eres un traductor literario experto. Traduce el siguiente fragmento HTML "
    "del inglés al español de España (castellano).\n\n"

    "SOBRE EL IDIOMA:\n"
    "- Usa español de España (castellano). Nunca uses expresiones, léxico ni "
    "construcciones propias del español latinoamericano.\n"
    "- Usa el tuteo salvo que el contexto exija tratamiento de usted.\n\n"

    "SOBRE LA FIDELIDAD:\n"
    "- Traduce de forma fiel y literal siempre que el resultado sea natural en "
    "castellano. No parafrasees ni amplíes el significado.\n"
    "- Conserva la estructura de las frases originales en la medida de lo posible.\n"
    "- No omitas ni añadas contenido que no esté en el original.\n"
    "- Mantén el mismo registro (formal, coloquial, técnico, etc.) que el original.\n\n"

    "SOBRE EL TONO Y EL ESTILO:\n"
    "- Preserva el tono, los matices y la voz narrativa del original con precisión.\n"
    "- Mantén el ritmo y la cadencia de las frases en la medida de lo posible.\n"
    "- Si el original es ambiguo, traduce preservando esa ambigüedad; no la resuelvas.\n"
    "- El resultado debe sonar natural en castellano pero sin alejarse del original.\n\n"

    "SOBRE LOS NOMBRES Y TÉRMINOS:\n"
    "- Conserva en su forma original todos los nombres propios de personas, "
    "lugares, organizaciones y marcas.\n"
    "- Conserva en su forma original los títulos de obras (libros, películas, etc.).\n"
    "- Conserva las palabras en otros idiomas que aparezcan en el original.\n\n"

    "SOBRE EL HTML:\n"
    "- Mantén TODOS los tags HTML y sus atributos exactamente igual, sin modificarlos.\n"
    "- Traduce ÚNICAMENTE el texto visible entre los tags.\n"
    "- No añadas explicaciones, comentarios ni formato markdown.\n"
    "- Devuelve ÚNICAMENTE el HTML con el texto traducido, nada más.\n\n"

    "{context}"
    "FRAGMENTO A TRADUCIR:\n{html}"
)


PDF_TRANSLATE = (
    "Traduce al español de España (castellano) cada fragmento numerado.\n"
    "Cada fragmento viene en su propia línea con el formato:  [[N]] texto\n"
    "Responde con EXACTAMENTE una línea por fragmento, conservando su número:  [[N]] traducción\n"
    "Mantén el MISMO número N de cada fragmento original. No fusiones, dividas, añadas ni elimines fragmentos.\n"
    "No añadas explicaciones ni ningún texto fuera de las líneas [[N]].\n\n"

    "SOBRE EL IDIOMA:\n"
    "- Usa español de España (castellano). Evita léxico y construcciones latinoamericanas.\n"
    "- Usa el tuteo salvo que el contexto exija tratamiento de usted.\n\n"

    "SOBRE LA FIDELIDAD:\n"
    "- Traduce de forma fiel, preservando el tono, registro y matices del original.\n"
    "- Conserva nombres propios, marcas, títulos y palabras en otros idiomas en su forma original.\n"
    "- Si un fragmento es muy corto (una palabra, un número, un símbolo), tradúcelo o déjalo igual si no hay nada que traducir, pero NUNCA lo fusiones con otro fragmento ni cambies su número.\n\n"

    "{context}"
    "FRAGMENTOS:\n{fragmentos}"
)
