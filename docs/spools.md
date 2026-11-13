# Spools and cost per print

Print Pantry keeps a spool inventory itself (no Spoolman container needed).

## How the grams are counted

Print Pantry can't weigh a spool, so it keeps books:

1. **Add a spool** on the Spools page with the filament on a full spool
   (1000 g for a 1 kg roll), what's left now, and the price you paid.
2. **When a print ends**, the grams the slicer worked out for each filament are
   taken off the spool in the toolhead that printed it. Cancelled and failed
   prints are charged by how far they got. (Print history reports finished
   jobs; prints started from the touchscreen count too.)
3. **Weigh a spool** now and then to correct drift: type in the kitchen-scale
   reading and the empty spool's weight (default 200 g, editable) is subtracted.

## Spools follow the toolheads

On the U1, each toolhead reports the material, colour and brand from the
spool's RFID tag. A toolhead with no spool gets the shelf spool with the same
material, a close colour and (if both say one) the same brand. When the printer
reports something different for a toolhead, its spool goes back on the shelf.
Use **Load / Move** to place a spool by hand; untick "Match it to a toolhead
automatically" on spools that should never be placed by themselves.

The print dialog shows the spool on each toolhead and warns when it has fewer
grams left than the file needs.

## Cost

- Filament: grams x the spool's price per gram. Files without a matching spool
  use the average price of your spools of that material, else the default
  price per kg.
- Electricity (optional): printer watts x print hours x price per kWh. Set the
  rate and each printer's average draw under **Cost settings**.

Model pages show an estimate (≈ $0.42) next to each print file's time and
weight. Finished prints in the history get the real grams and cost.
