---PEP8


*PEP8: Proje geliştirilirken PEP8 kullanacağız.

*Note: Bu dökümanı VS Code ile açın.

*1. Dil: Değişkenler, fonksiyonlar ve yorumlar %100 İngilizce olmalıdır.

*2. İsimlendirmeler:

    variable_name: Değişkenler küçük harf, alt tire
    function_name: Fonksiyonlar küçük harf, alt tire
    ClassName: Sınıflar, Büyük harfle başlar, bitişik
    CONSTANT_NAME: Sabitler BÜYÜK_HARF, alt tire
    Girintileme: Tab tuşu değil, 4 boşluk (Space) kullanılmalıdır.

# ✅ Doğru:
def calculate_screen_center(width: int, height: int):
    center_x = width // 2
    return center_x
# ❌ Yanlış:
def EkranOrtasi(w, h): # Türkçe ve belirsiz isimler yasak
    x = w/2 
    return x

*3. MCP & LLM İçin Kritik Kurallar:
    Bu proje MCP kullandığı için, fonksiyonlarımız aslında birer "Araç"tır (Tool). LLM'in bu araçları kullanabilmesi için şunlar zorunludur.

    *3.1. Type Hinting (Tip Belirleme):
        Her fonksiyonun parametreleri ve dönüş değeri mutlaka tip (type) içermelidir. MCP sunucusu bu tiplere bakarak JSON şeması oluşturur.

        # ✅ Örnek:
        def move_mouse(x: int, y: int) -> str:
            # ...kodlar...

    *3.2. Docstrings (Fonksiyon Kimlik Kartları):
        MCP projesi olduğumuz için, yazdığınız Docstring'ler Yapay Zeka tarafından okunur. Eğer burayı kötü yazarsanız, Ajan (Agent) o aracı (Tool) kullanamaz.

        Format (Google Style veya NumPy Style):
        
        Özet: Ne işe yarar?
        Args: Parametreler nedir ve tipleri nedir?
        Returns: Ne döndürür?
        Important/Warning: Varsa kritik uyarılar.

        # ✅ Örnek:
        def focus_window(app_name: str) -> str:
            """
            Brings the window containing the specific text to the foreground.
            
            This is critical to use before typing any text.
            
            Args:
                app_name (str): Partial name of the window (e.g., 'Notepad', 'Chrome').
                
            Returns:
                str: Result message indicating success or failure.
            """
            # ... kodlar ...



*4. Hata Yönetimi (Error Handling):
    Ajan (Agent) bir hata aldığında program çökmemeli, hatayı anlayıp başka bir yol denemelidir.

    Asla sessiz kalma: pass kullanmak yasaktır.
    Açıklayıcı Hata Dön: Fonksiyonlar hata durumunda False değil, hatanın sebebini anlatan bir str (String) dönmelidir.

    # ✅ Doğru:
    try:
        # ... işlem ...
        return "Success: Clicked button."
    except Exception as e:
        return f"Error: Failed to click because {str(e)}"

*5. Git & Versiyon Kontrol Kuralları:

    *5.1 Commit Mesajları: Ne yaptığınızı net bir şekilde ifade edin. (Conventional Commits standardı tercih edilir).

        feat: Add OmniParser local support (Yeni özellik)
        fix: Resolve timeout issue in client (Hata düzeltme)
        docs: Update README file (Dokümantasyon)
        refactor: Clean up mouse.py code (Kod temizliği)

        -> 
        feat Commits that add, adjust or remove a new feature to the API or UI
        fix Commits that fix an API or UI bug of a preceded feat commit
        refactor Commits that rewrite or restructure code without altering API or UI behavior
        perf Commits are special type of refactor commits that specifically improve performance
        style Commits that address code style (e.g., white-space, formatting, missing semi-colons) and do not affect application behavior
        test Commits that add missing tests or correct existing ones
        docs Commits that exclusively affect documentation
        build Commits that affect build-related components such as build tools, dependencies, project version, ...
        ops Commits that affect operational aspects like infrastructure (IaC), deployment scripts, CI/CD pipelines, backups, monitoring, or recovery procedures, ...
        chore Commits that represent tasks like initial commit, modifying .gitignore, ...

    *5.2 Branch: Asla main dalına doğrudan push atmayın. Kendi branch'inizi açın.

*6. Güvenlik (Security):

    API Key: Kodun içine asla sk-proj... gibi API anahtarları yazmayın. Her zaman os.getenv("KEY_NAME") kullanarak .env dosyasından çekin.
    Dosya Yolları: Bilgisayarınıza özel yollar (C:/Users/Ahmet/Desktop/...) kullanmayın. os.path modülünü kullanın.

*7. Satır İçi Yorumlar (Inline Comments):

    Kural: Sadece karmaşık mantığı açıklamak için kullanın. Cümleye büyük harf ile başlayın.
    Yerleşim: Koddan en az 2 boşluk sonra  # ile başlayın.
    Gereksiz Yorum Yapmayın: Kodu okuyan kişi zaten yazılımcıdır.

    # ❌ Yanlış:
    x = x + 1  # x'i bir artır

    # ✅ Doğru:
    # Threshold 0.8 because low-light conditions affect detection accuracy
    if confidence > 0.8:
        click_target()

    *7.1. Etiketleme : 
        Projeyi geliştirirken eksik veya hatalı yerleri unutmamak için standart etiketler kullanın.
        
        # TODO: Yapılacak ama acil olmayan işler.
        # FIXME: Çalışıyor ama bozuk veya hatalı, hemen düzeltilmeli.
        # NOTE: Dikkat edilmesi gereken önemli bir bilgi.
        # OPTIMIZE: Çalışıyor ama performansı artırılabilir.

        İlgili döküman: https://www.conventionalcommits.org/en/v1.0.0/

        # ✅ Örnek:
        def capture_screen():
            # TODO: Add multi-monitor support later
            # FIXME: This crashes on Linux systems

*8. Proje Yapısı:
    Kodlarınızı rastgele yerlere atmayın.

VILAGENT/ (Proje Ana Dizini)
├── .env                    # [GİZLİ] API Anahtarları ve Hassas Ayarlar
├── .gitignore              # Git'e yüklenmeyeceklerin listesi
├── CODING_STANDARDS.md     # Kodlama Kuralları Rehberi
├── README.md               # Proje Kurulum ve Kullanım Kılavuzu
├── requirements.txt        # Python Kütüphane Listesi
│
├── data/                   # Dinamik verilerin yaşayacağı yer
│   ├── chroma_db/          # Vektör veritabanı (Binary dosyalar)
│   └── logs/               # Uygulama çalışma logları
│
├── docs/                   # Proje dokümantasyonu (Yazılımcılar için)
│   ├── architecture.md     # Mimari şemalar
│   └── setup.md            # Kurulum detayları
│
├── model_data/             # [GİT DIŞI] Büyük Model Dosyaları (>100MB)
│   ├── omniparser/         # .pt, .onnx dosyaları
│   └── yolo/               # .pt dosyaları
│
├── scripts/                # Yardımcı ve Kurulum Scriptleri (Uygulama dışı)
│   ├── setup_env.ps1       # Ortamı hazırlayan script
│   └── download_models.py  # Ağırlıkları indiren script
│
└── src/                    # Çekirdek (Python Kodları)
    ├── __init__.py
    │
    ├── clients/            # Ajanı Başlatan Kodlar (Orkestra Şefi)
    │   ├── client_groq.py  # Groq tabanlı ajan
    │   └── client_vision.py  # Vision tabanlı ajan
    │
    ├── model_handlers/     # Modelleri Yöneten Kodlar (Köprü)
    │   ├── yolo_handler.py # model_weights/ klasöründen okuma yapar
    │   └── omni_handler.py # OmniParser mantığını çalıştırır
    │   └── model_configs/  # Modellerin config'leri
    │       └── omni_config.py  # Omniparser config
    │
    ├── prompts/            # LLM Sistem Talimatları (.yaml/.txt)
    │   ├── system.yaml     # "Sen bir bilgisayar ajanısın..."
    │   └── tasks.yaml      # Özel görev tanımları
    │
    ├── servers/            # MCP Sunucuları (Eller ve Gözler)
    │   ├── control/
    │   │   ├── control_sv.py
    │   │   └── tools/      # Klavye/Fare iş mantığı
    │   │
    │   └── vision/
    │       ├── vision_sv.py
    │       └── tools/      # Görüntü işleme araçları
    │           └── omni_tool.py # Mesela model_handlers'ı çağırır
    │
    └── shared/             # Ortak Parçalar (İsviçre Çakısı)
        ├── vilagent_config.py  # Proje Sabitleri (PATH ayarları burada yapılır)
        ├── dataclasses.py  # Veri Modelleri (Dataclasses)
        └── utils.py        # Loglama, Resim çevirme vb.