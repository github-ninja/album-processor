"""
Album processor: organises new downloads (any source) into the music library and keeps
the library tidy.

Library conventions (tags are the source of truth, names derive from them):
  <MUSIC_PATH>/<Album artist>/<YYYY - Album>/NN. <Artist> - <Title>.flac   (16 bit / 44.1 kHz)
  <MUSIC_PATH>/<Album artist>/<YYYY - Album>/Cover.jpg   (1000×1000 JPEG)

Modules:
  config     — paths and switches from environment variables
  names      — title cleanup, file/folder naming rules, fuzzy matching
  files      — filesystem helpers and the change journal
  tags       — reading/writing tags and embedded pictures
  discogs    — album title, year, genre and tracklist lookup
  covers     — cover search (Deezer), comparison and normalisation
  audioformat — conversion of hi-res FLAC, ALAC, APE and WAV to FLAC 16/44.1
  cue        — splitting of a whole-album image by its CUE sheet
  replaygain — ReplayGain 2.0 tags (EBU R128 loudness, true peaks)
  upconvert  — spectral detection of lossy→lossless upconverts
  pipeline   — processing of new downloads
  library    — checks and fixes for albums already in the library
  cli        — command-line entry point
"""
