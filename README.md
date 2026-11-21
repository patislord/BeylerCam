# BeylerCam

Telefon kamerasını Wi-Fi üzerinden PC'ye bağlar; görüntü PC'de hem önizleme penceresinde görünür
hem de **sanal webcam** olarak Zoom, Discord, Teams, OBS gibi programlarda kullanılabilir.

```
┌──────────── Android ────────────┐        Wi-Fi         ┌──────────────── PC (Windows) ────────────────┐
│ CameraX → YUV → JPEG            │  TCP 4747 (kareler)  │ JPEG çöz → döndür/ayna → önizleme penceresi │
│ TCP sunucu + UDP keşif (4747)   │ ───────────────────► │                        └→ sanal webcam (OBS)  │
│ Ayarları PC'den alır            │ ◄─────────────────── │ çözünürlük/FPS/kalite/kamera komutları       │
└─────────────────────────────────┘                      └──────────────────────────────────────────────┘
```

## Özellikler
- Ön/arka kamera değiştirme (PC'den veya telefondan)
- Çözünürlük (640x360 – 1920x1080), FPS (15/24/30/60) ve JPEG kalitesi ayarı (çalışırken değişir)
- Ayna (yatay çevirme) ve 90° döndürme
- "Telefonu Bul" ile ağdaki telefonu otomatik bulma
- Koparsa otomatik yeniden bağlanma; FPS, Mbit/s ve gecikme (RTT) göstergesi
- Fotoğraf kaydetme (`Resimler\BeylerCam`)
- Telefonda "Ekranı karart" modu (önizleme kapanır, pil ve ısınma azalır)

## Kurulum

### 1) PC
1. Sanal webcam için **OBS Studio** kurun (sürücüsü onunla gelir):
   `winget install OBSProject.OBSStudio`
   OBS'i açık tutmanız gerekmez. Sadece önizleme isterseniz bu adımı atlayabilirsiniz.
2. `pc\BeylerCam.bat` dosyasına çift tıklayın. İlk açılışta Python 3.14 ortamı ve paketler
   otomatik kurulur (birkaç dakika sürer). Sonraki açılışlar anında olur.

### 2) Telefon (Android 8.0+)
Hazır APK: **`BeylerCam.apk`** (proje kök klasöründe).
Telefona atıp (USB, Drive, WhatsApp vb.) açın. "Bilinmeyen kaynaklardan yüklemeye izin ver" sorusuna izin verin.

Kodu değiştirip yeniden derlemek için:
- **Android Studio**: `android` klasörünü açın → ▶ Run.
- **Komut satırı** (bu PC'de kurulu derleme ortamıyla):
  ```powershell
  $env:JAVA_HOME = "$env:LOCALAPPDATA\BeylerCamBuild\jdk\jdk-21.0.13.1+2"
  cd android; .\gradlew.bat assembleDebug
  # çıktı: android\app\build\outputs\apk\debug\app-debug.apk
  ```

## Kullanım
1. Telefon ve PC **aynı Wi-Fi ağında** olsun.
2. Telefonda BeylerCam'i açın, kamera iznini verin. Ekranda IP adresi görünür (ör. `192.168.1.23 : 4747`).
3. PC'de **Telefonu Bul**'a basın (ya da IP'yi elle yazın) → **Bağlan**.
4. Sanal webcam için **Sanal webcam'i etkinleştir**'i işaretleyin. Zoom/Discord/Teams'te kamera olarak
   **"OBS Virtual Camera"** seçin.

> Telefon yatay tutulduğunda görüntü düz gelir. Telefonu dikey kullanıyorsanız **Sağa/Sola 90°** ile düzeltin.
> Sanal webcam her zaman seçilen çözünürlükte yatay çıkış verir; dikey görüntü kenarları siyah olacak şekilde ortalanır.

## Sorun giderme
| Sorun | Çözüm |
|---|---|
| "Telefon bulunamadı" | Telefonda uygulama açık mı, aynı Wi-Fi'da mı? Misafir ağları cihazları birbirinden izole edebilir. IP'yi elle girmeyi deneyin. |
| İlk bağlantıda Windows Güvenlik Duvarı sorusu | "Özel ağlar" için izin verin. |
| "OBS Virtual Camera bulunamadı" | OBS Studio'yu kurun. Kuruluysa OBS içindeki "Sanal Kamerayı Başlat" kapalı olmalı. |
| Görüntü takılıyor | Çözünürlüğü veya kaliteyi düşürün, 5 GHz Wi-Fi kullanın. |
| "Telefondan görüntü gelmiyor" | Telefonda uygulama ön planda olmalı (ekranı karartabilirsiniz ama uygulamadan çıkmayın). |

## Geliştirme
```
pc\
  main.py                 giriş noktası
  beylercam\app.py        Tkinter arayüzü
  beylercam\client.py     telefona bağlanan istemci (yeniden bağlanma, ping)
  beylercam\protocol.py   ağ protokolü
  beylercam\discovery.py  UDP ile telefon bulma
  beylercam\vcam.py       sanal webcam (pyvirtualcam)
  beylercam\simulator.py  sahte telefon (test için)
  tests\                  uçtan uca testler
android\app\src\main\java\com\beyler\cam\
  MainActivity.kt         ekran, izinler
  CameraStreamer.kt       CameraX, çözünürlük/FPS, JPEG
  YuvJpegEncoder.kt       YUV_420_888 → JPEG
  StreamServer.kt         TCP sunucu (en yeni kare önceliği)
  DiscoveryResponder.kt   UDP keşif cevabı
```

Telefon olmadan deneme:
```
cd pc
.venv\Scripts\python -m beylercam.simulator --rotation 90   # sahte telefon
.venv\Scripts\python main.py                                # IP: 127.0.0.1
.venv\Scripts\python -m unittest -v                         # testler
```

Protokol ayrıntıları `pc\beylercam\protocol.py` ve `Protocol.kt` başında yazılıdır.

**Güvenlik notu:** DroidCam gibi, aynı ağdaki herkes 4747 portuna bağlanıp görüntüyü alabilir.
Telefon ekranında bağlı PC'lerin IP adresleri gösterilir. Uygulamayı yalnızca güvendiğiniz ağlarda kullanın.
