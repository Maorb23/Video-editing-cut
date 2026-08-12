# Existing MLT projects

Treat an input project as immutable. Parse it with an XML API, verify its root
and profile, clone all existing elements/properties, and append generated nodes
under reserved `ves_` IDs. Refuse reserved-ID collisions and profile mismatch.

The V1 importer does not edit arbitrary existing producers, filters,
transitions, playlists, or tractors. Any operation target prefixed `base:` is
an explicit unsupported intersection and must fail. This prevents silent
flattening or loss. Unknown XML survives semantically, but XML declaration,
indentation, attribute order, and empty-element syntax can change on output.

