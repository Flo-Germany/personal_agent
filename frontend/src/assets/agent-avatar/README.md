# Agent profile picture

Drop **one** image in this folder and it becomes Compa's avatar everywhere
(header, empty state, message rows, tool rows, Identity & Memory card).

Supported names/formats (Vite picks the first match alphabetically):

```text
avatar.png
avatar.svg
avatar.jpg
avatar.jpeg
avatar.webp
```

Recommended:

- square image, **256 × 256 px** or larger (it is displayed at 28–64 px)
- transparent background works best (the avatar is rendered in a circle)
- `.svg` is ideal for a flat logo, `.png` for a rendered picture

Notes:

- The folder is intentionally empty: without an image the built-in
  gradient + sparkle avatar is used.
- The image is bundled at build time, so restart `npm run tauri dev` or rebuild
  the app after adding or replacing it.
- To remove a custom picture, delete the file again.
