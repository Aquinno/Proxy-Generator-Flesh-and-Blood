# Proxy Temple v16

Unofficial fan-made Flesh and Blood proxy-printing utility.

## Search flow
1. Enter a card name or search term.
2. Proxy Temple queries the same Card Vault `advanced-search` backend used by the Card Vault results page.
3. Every returned `card_id` is shown as a separate clickable card result. No colour guessing, no `-1/-2/-3` probing, and no exact-name shortcut.
4. Clicking a result loads that exact card and its print variations.
5. Select a print and quantity, add it to the list, then generate the A4 PDF.

Card Vault's current API documentation/analysis confirms that its results page uses `advanced-search`, while `card_id/<id>/` returns the card's `card_prints`.

## Run
Run `start.bat`.

## Print layout
- A4 portrait
- 3 x 3 cards
- 63 x 88 mm per card
- Rounded corners approximately 3 mm
- Original large card image resolution preserved

## Notice
This is an unofficial fan-made utility. Flesh and Blood, card names, artwork, logos, and related trademarks are property of their respective owners, including Legend Story Studios. This project is not affiliated with, endorsed by, or sponsored by Legend Story Studios.
