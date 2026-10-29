# Arena wall billboards

Drop ad images here to texture the arena walls in the MicroDuck 3D view:

```
side_px_1.png … side_px_6.png
side_nx_1.png … side_nx_6.png
side_py_1.png … side_py_6.png
side_ny_1.png … side_ny_6.png
```

- Side names match the physics walls (`px`/`nx`/`py`/`ny`).
- Up to 6 images per side; `.png` is tried first, then `.jpg`.
- Numbering sets left-to-right order; gaps are fine (missing files are
  skipped and the rest spread evenly).
- Images are contain-fit into their slot — never stretched. The wall is
  4.4 m long × 0.3 m tall, so with 6 panels each slot is ~0.7 m wide;
  landscape artwork around 2:1–3:1 fills best.
- A side with no images keeps its plain wall.
