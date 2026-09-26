// Synthetic fragmented-MP4 fixtures for the live photo avatar player tests.
// They reproduce only the structure the player reads (box framing, the avcC
// profile bytes and the esds audio object type); the payloads are not media.

const encoder = new TextEncoder();

export function concatBytes(...parts: Uint8Array[]): Uint8Array {
  const total = parts.reduce((sum, part) => sum + part.length, 0);
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    joined.set(part, offset);
    offset += part.length;
  }
  return joined;
}

export function box(type: string, ...parts: Uint8Array[]): Uint8Array {
  const body = concatBytes(...parts);
  const out = new Uint8Array(8 + body.length);
  new DataView(out.buffer).setUint32(0, out.length);
  out.set(encoder.encode(type), 4);
  out.set(body, 8);
  return out;
}

const bytes = (...values: number[]) => new Uint8Array(values);
const zeros = (count: number) => new Uint8Array(count);

function u32(value: number): Uint8Array {
  const out = new Uint8Array(4);
  new DataView(out.buffer).setUint32(0, value);
  return out;
}

function avcEntry(profile: number, compatibility: number, level: number): Uint8Array {
  // 78 bytes of VisualSampleEntry fields, then the avcC configuration record.
  return box("avc1", zeros(78), box("avcC", bytes(1, profile, compatibility, level, 0xff, 0xe1)));
}

function mp4aEntry(objectType: number): Uint8Array {
  const specific = bytes(0x05, 2, (objectType << 3) | 0x01, 0x90);
  const config = bytes(0x04, 13 + specific.length, 0x40, 0x15, ...zeros(11), ...specific);
  const es = bytes(0x03, 3 + config.length, 0x00, 0x01, 0x00, ...config);
  // 28 bytes of AudioSampleEntry fields, then esds (version/flags + ES_Descriptor).
  return box("mp4a", zeros(28), box("esds", zeros(4), es));
}

function trak(entry: Uint8Array): Uint8Array {
  const stsd = box("stsd", zeros(4), u32(1), entry);
  return box("trak", box("tkhd", zeros(84)), box("mdia", box("minf", box("stbl", stsd))));
}

export interface InitSegmentOptions {
  profile?: number;
  compatibility?: number;
  level?: number;
  audioObjectType?: number | null;
  withAvcC?: boolean;
}

/** An ftyp+moov init segment shaped like the Voice Live avatar stream. */
export function initSegment({
  profile = 0x64,
  compatibility = 0x00,
  level = 0x1e,
  audioObjectType = 2,
  withAvcC = true,
}: InitSegmentOptions = {}): Uint8Array {
  const video = withAvcC
    ? trak(avcEntry(profile, compatibility, level))
    : trak(box("avc1", zeros(78)));
  return concatBytes(
    box("ftyp", encoder.encode("iso5"), zeros(4), encoder.encode("iso6mp41")),
    box(
      "moov",
      box("mvhd", zeros(100)),
      video,
      ...(audioObjectType === null ? [] : [trak(mp4aEntry(audioObjectType))]),
      box("mvex", box("trex", zeros(24))),
    ),
  );
}

/** One moof+mdat media fragment; `sequence` tags the bytes for ordering checks. */
export function mediaFragment(sequence: number): Uint8Array {
  return concatBytes(
    box("moof", box("mfhd", zeros(4), u32(sequence))),
    box("mdat", u32(sequence), bytes(1, 2, 3)),
  );
}

export function toBase64(data: Uint8Array): string {
  let binary = "";
  for (const byte of data) binary += String.fromCharCode(byte);
  return btoa(binary);
}

/** The sequence number a mediaFragment() carries, read back from appended bytes. */
export function fragmentSequence(data: Uint8Array): number | null {
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
  // moof(8) + mfhd header(8) + version/flags(4) = 20 bytes before the sequence.
  if (data.length < 24) return null;
  const type = String.fromCharCode(data[4], data[5], data[6], data[7]);
  return type === "moof" ? view.getUint32(20) : null;
}
