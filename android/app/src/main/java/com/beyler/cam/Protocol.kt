package com.beyler.cam

/**
 * BeylerCam ağ protokolü (v1). PC tarafı: pc/beylercam/protocol.py
 *
 * TCP (port 4747):
 *   Telefon -> PC : [tip: u8][uzunluk: u32 big-endian][veri]
 *       HELLO  (1): JSON  {"app","version","device","cameras",...status}
 *       FRAME  (2): [dönüş derecesi: u16][bayraklar: u8 (bit0 = ön kamera)][JPEG]
 *       STATUS (3): JSON  {"width","height","fps","quality","camera",...}
 *       PONG   (4): JSON  {"t": <ping'deki değer>}
 *   PC -> Telefon : UTF-8 JSON satırları ('\n' ile biter)
 *       {"cmd":"config","width":1280,"height":720,"fps":30,"quality":80,"camera":"back"}
 *       {"cmd":"switch"}
 *       {"cmd":"ping","t":123}
 *
 * UDP (port 4747): PC "BEYLERCAM_DISCOVER" yayınlar, telefon JSON ile cevap verir.
 */
object Protocol {
    const val PORT = 4747
    const val VERSION = 1
    const val APP = "BeylerCam"

    const val TYPE_HELLO = 1
    const val TYPE_FRAME = 2
    const val TYPE_STATUS = 3
    const val TYPE_PONG = 4

    const val FLAG_FRONT = 0x01

    const val DISCOVER_MAGIC = "BEYLERCAM_DISCOVER"
}
