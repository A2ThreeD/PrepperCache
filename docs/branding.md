# PrepperCache branding and appearance

Use **PrepperCache** as the product name. The header artwork pairs Mt. Hood with the equipment case and PrepperCache wordmark. Keep the artwork at its original proportions and use a compact header so the portal content stays in view. On Charcoal / Blue, blend the outer edges of the artwork into the header and use muted blue borders on the dashboard cards.

| Appearance | Background | Cards | Main text | Secondary text | Accent |
| --- | --- | --- | --- | --- | --- |
| Charcoal / Blue (default) | `#151B21` | `#212A33` | `#F4F8FA` | `#B7C7D2` | `#38B6FF` |
| Light | `#F3F7F6` | `#FFFFFF` | `#172B2D` | `#496163` | `#006E68` |

Charcoal / Blue uses the electric-blue header (`images/preppercache-blue.png`). Light uses the teal header (`images/preppercache.png`). Both themes keep the system sans-serif font so the portal works offline. Category icons keep their own colors, including red for First Aid. Use the shared house icon for Home buttons.

Choose a theme in **Appearance** at the top right of **Settings**. PrepperCache remembers the choice in the current browser and applies it to Home, Settings, Maps, the library placeholder, and the portal's progress panel. The map basemap has its own setting under **Settings → Maps**.

The Charcoal / Blue palette and shared component styling live in [theme.css](../web/theme.css). Page styles use those tokens with Light fallback values. Use blue for links, focus, selection, and progress, with restrained panel borders and shadows. Blue primary buttons use dark text for contrast. Keep green, amber, and red for success, warning, and error, and pair each status color with a readable label. Check normal text against its actual background for at least 4.5:1 contrast.
