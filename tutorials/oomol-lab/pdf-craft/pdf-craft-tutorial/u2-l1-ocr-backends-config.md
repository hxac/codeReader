# 六种 OCR 后端配置

## 1. 本讲目标

在上一讲中,我们已经知道 `PDFCraft` 门面通过 `PDFOptions(ocr=...)` 接收 OCR 配置,并且 OCR 引擎是延迟加载的。本讲深入这个 `ocr=` 参数背后的世界:整个 `pdf_craft/ocr_config.py` 文件。

学完本讲,你应该能够:

1. 区分 **local(本地)** 与 **vendor(远程服务)** 两类 OCR 配置的运行位置、依赖与适用场景。
2. 掌握三族模型(DeepSeek OCR、DeepSeek OCR 2、Unlimited OCR)× 两种运行位置共 **六种配置对象** 的构造参数与默认值。
3. 理解 `ensure_ocr_config` 函数的两大职责:**互斥校验**(显式配置与便捷字段不能同时给)与**兜底策略**(什么都没给时默认使用本地 DeepSeek OCR)。

## 2. 前置知识

### OCR 是什么

OCR(Optical Character Recognition,光学字符识别)把图像里的文字「认」出来。pdf-craft 的输入通常是扫描版 PDF——每一页本质上是一张图片,计算机并不知道图片里有哪些字。OCR 就是把「页面图片」变成「带坐标的文本块」的过程。在第一讲我们提过一句口诀:**OCR 负责认字,LLM 负责翻译**,两者是独立配置、互不干扰的。本讲只关心「认字」这一半。

### 声明式配置与 frozen dataclass

pdf-craft 的配置对象是**声明式**的:它只描述「用什么模型、怎么连接」,本身不包含任何运行逻辑。所有配置类都用 `@dataclass(frozen=True)` 修饰:

- `dataclass` 让 Python 自动生成 `__init__`、`repr` 等方法,省去样板代码。
- `frozen=True` 表示**冻结**——对象创建后不能再修改字段。配置一旦传入库内部,任何代码都无法偷偷改掉它,这让配置在多次提取之间是安全可复用的。

### TypeAlias 与 Literal

`TypeAlias`(类型别名)给一个已有类型起个短名字,运行时零开销;`Literal["a", "b"]` 表示「只允许这几个字面量字符串」的类型。它们只存在于类型检查阶段,帮编辑器和 mypy 提前发现拼写错误,不影响运行时行为。

### 两类远程鉴权方式

- **OpenAI 兼容 API**:很多模型服务都采用「`base_url` + `api_key` + `model`」这组参数,形如 OpenAI 官方接口。只要服务兼容这套协议,换 `base_url` 就能换供应商。
- **百度云 ak/sk**:Access Key + Secret Key 成对使用,是百度智能云的鉴权方式。这与 OpenAI 风格完全不同,所以 Unlimited OCR 的远程配置字段长得不一样。

### GPU 与 CUDA

本地跑视觉 OCR 模型需要 NVIDIA GPU,并安装 CUDA 运行库。没有 GPU 的机器只能选择远程(vendor)配置——这正是 pdf-craft 提供两类配置的根本原因。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pdf_craft/ocr_config.py` | **本讲主角**:六个配置类、四个类型别名、`ensure_ocr_config` 函数 |
| `pdf_craft/craft.py` | `PDFOptions` 定义处,`ocr` 字段的入口;以及惰性组装引擎的 `_pdf_engine` |
| `pdf_craft/transform.py` | `PDFExtractionEngine` 构造时调用 `ensure_ocr_config`(调用点一) |
| `pdf_craft/functions.py` | `predownload_models` 工具函数,同样调用 `ensure_ocr_config`(调用点二) |
| `pdf_craft/pdf/page_extractor.py` | 配置的**消费侧**:用 `isinstance` 把六种配置映射到真实 OCR 后端 |
| `pdf_craft/to_path.py` | 本地配置的路径归一化工具 `to_path` |
| `docs/en/OCR_BACKENDS.md` | 官方 OCR 后端指南,含六种配置对照表 |

## 4. 核心概念与源码讲解

### 4.1 配置数据类:三族模型 × 两种运行位置

#### 4.1.1 概念说明

pdf-craft 支持三族 OCR 模型,每族都可以选择「在本机 GPU 上跑」或「调用远程服务」,于是得到 3 × 2 = 6 种配置类:

| 配置类 | 模型族 | 运行位置 |
| --- | --- | --- |
| `DeepSeekOCRLocalConfig` | DeepSeek OCR | 本地 GPU |
| `DeepSeekOCR2LocalConfig` | DeepSeek OCR 2 | 本地 GPU |
| `UnlimitedOCRLocalConfig` | Unlimited OCR | 本地 GPU |
| `DeepSeekOCRVendorConfig` | DeepSeek OCR | OpenAI 兼容远程服务 |
| `DeepSeekOCR2VendorConfig` | DeepSeek OCR 2 | OpenAI 兼容远程服务 |
| `UnlimitedOCRVendorConfig` | Unlimited OCR | 百度远程服务 |

这个对照表来自官方文档 [docs/en/OCR_BACKENDS.md:7-14](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/OCR_BACKENDS.md#L7-L14),它用表格列出了六种配置与模型、运行位置的对应关系。

**为什么用六个类而不是一个带 `mode` 字段的类?** 因为 local 与 vendor 需要的参数完全不同(路径与设备号 vs. 凭据与网络参数),不同模型族的凭据风格也不同(OpenAI 风格 vs. 百度 ak/sk)。六个 frozen dataclass 让每种配置的必填参数在**类型层面**就是明确的:构造 `DeepSeekOCRVendorConfig` 时忘传 `api_key`,Python 会立刻报错,而不用等到运行时才发现。

#### 4.1.2 核心流程

一个配置对象的生命周期:

```text
用户构造配置对象(如 DeepSeekOCRVendorConfig(...))
        │
        ▼
塞进 PDFOptions(ocr=...) ────────── 冻结、只读
        │
        ▼
首次提取时惰性组装 PDFExtractionEngine
        │  (调用 ensure_ocr_config 归一化,见 4.3)
        ▼
OCR / PageExtractorNode 按 isinstance 分派到真实后端(见 4.2)
```

配置对象全程只被「读取」,从不被修改——这是 frozen dataclass 与声明式配置配合的结果。

#### 4.1.3 源码精读

**(1) 三个本地配置类:同构的三兄弟**

以 `DeepSeekOCRLocalConfig` 为例,三个本地配置类的字段完全一致:[pdf_craft/ocr_config.py:9-31](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/ocr_config.py#L9-L31) 定义了它,包含三个字段并自定义了 `__init__`:

```python
@dataclass(frozen=True)
class DeepSeekOCRLocalConfig:
    models_cache_path: Path | None = None
    local_only: bool = False
    enable_devices_numbers: tuple[int, ...] | None = None
```

- `models_cache_path`:模型缓存的存放目录。首次使用时模型权重会下载到这里;之后离线也能跑。
- `local_only`:为 `True` 时**禁止下载**缺失的模型。官方文档 [docs/en/OCR_BACKENDS.md:31-33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/OCR_BACKENDS.md#L31-L33) 提醒:只在确认模型已在缓存中时才开启,否则会直接失败。
- `enable_devices_numbers`:使用哪些 GPU 设备(对应进程可见的 CUDA 设备号)。

另外两个本地类 `DeepSeekOCR2LocalConfig`([pdf_craft/ocr_config.py:34-56](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/ocr_config.py#L34-L56))与 `UnlimitedOCRLocalConfig`([pdf_craft/ocr_config.py:59-81](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/ocr_config.py#L59-L81))字段与行为完全相同——三兄弟只靠**类名**区分,消费侧正是用类名(isinstance)来决定加载哪个模型的。

**为什么 frozen 类还要手写 `__init__`?** 看 [pdf_craft/ocr_config.py:15-31](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/ocr_config.py#L15-L31):

```python
def __init__(self, models_cache_path=..., local_only=False, enable_devices_numbers=None):
    object.__setattr__(
        self, "models_cache_path",
        to_path(models_cache_path) if models_cache_path is not None else None,
    )
    object.__setattr__(self, "local_only", local_only)
    object.__setattr__(
        self, "enable_devices_numbers",
        tuple(enable_devices_numbers) if enable_devices_numbers is not None else None,
    )
```

两个原因:

1. **做参数归一化**。`frozen=True` 禁止普通的 `self.x = x` 赋值,所以必须绕道 `object.__setattr__`。借这次手写,顺手把 `str` 路径转成绝对 `Path`、把任意可迭代对象固化成 `tuple`。
2. **对外接口更宽容**。用户可以传 `"models-cache"` 字符串或 `[0]` 列表,库内部拿到的永远是规范类型。

路径归一化由 [pdf_craft/to_path.py:5-9](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/to_path.py#L5-L9) 的 `to_path` 完成:相对路径会拼上当前工作目录再 `resolve()`,避免「在哪个目录运行」影响缓存位置。

**(2) DeepSeek 族远程配置:OpenAI 兼容三件套**

[pdf_craft/ocr_config.py:84-92](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/ocr_config.py#L84-L92) 定义 `DeepSeekOCRVendorConfig`(`DeepSeekOCR2VendorConfig` 在 [pdf_craft/ocr_config.py:95-103](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/ocr_config.py#L95-L103),字段完全相同):

```python
@dataclass(frozen=True)
class DeepSeekOCRVendorConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int = 8000
    timeout_seconds: int = 180
```

- `base_url` / `api_key` / `model` 是必填的三件套,没有默认值——忘传就构造失败,错误在最早处暴露。
- `api_key` 用 `field(repr=False)` 标记,生成的 `repr` 会**隐藏密钥**。这样打印配置对象、或配置对象意外出现在日志与异常信息里时,不会泄漏凭据(这与第一讲 `PDFOptions` 中凭据的防护思路一致)。
- `temperature`、`top_p` 是采样参数;`max_tokens`(默认 8000)限制单次输出长度;`timeout_seconds`(默认 180)是请求超时。OCR 页面识别输出较大,所以这两个默认值都偏宽松。

**(3) Unlimited OCR 远程配置:百度风格**

[pdf_craft/ocr_config.py:106-112](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/ocr_config.py#L106-L112) 定义 `UnlimitedOCRVendorConfig`:

```python
@dataclass(frozen=True)
class UnlimitedOCRVendorConfig:
    ak: str = field(repr=False)
    sk: str = field(repr=False)
    base_url: str = "https://aip.baidubce.com"
    poll_interval_seconds: float = 2.0
    timeout_seconds: int = 180
```

- `ak` / `sk` 是百度云的访问密钥对,同样被 `repr=False` 保护。
- `base_url` 默认指向百度智能云,通常无需修改。
- 多了一个 DeepSeek 族没有的参数 `poll_interval_seconds`(轮询间隔,默认 2 秒)。从参数命名可以推断:该服务的识别是**异步任务式**的——提交页面后需要定期查询结果,这个参数控制两次查询之间的等待时间。

**(4) 四个类型别名与 OCRMode**

[pdf_craft/ocr_config.py:115-129](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/ocr_config.py#L115-L129) 把六个类组织成清晰的层次:

```python
LocalOCRConfig: TypeAlias = (DeepSeekOCRLocalConfig | DeepSeekOCR2LocalConfig | UnlimitedOCRLocalConfig)
VendorOCRConfig: TypeAlias = (DeepSeekOCRVendorConfig | DeepSeekOCR2VendorConfig | UnlimitedOCRVendorConfig)
OCRConfig: TypeAlias = LocalOCRConfig | VendorOCRConfig
OCRMode: TypeAlias = Literal[
    "deepseek-ocr-local", "deepseek-ocr2-local", "unlimited-ocr-local",
    "deepseek-ocr-vendor", "deepseek-ocr2-vendor", "unlimited-ocr-vendor",
]
```

- `OCRConfig` 是「六选一」的联合类型,正是 `PDFOptions.ocr` 字段的类型。
- `OCRMode` 用六个字符串字面量给每种模式起了名字(如 `"deepseek-ocr-local"`),供类型检查用;运行时它就是普通 `str`。仓库本地 CLI(`pdf_craft_tool`)正是用这种字符串来指定 OCR 模式的。

这六个类和别名全部从包顶层导出,见 [pdf_craft/__init__.py:25-36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L25-L36),用户直接 `from pdf_craft import DeepSeekOCRVendorConfig` 即可。

#### 4.1.4 代码实践

**实践:构造六种配置,观察 repr 与参数归一化**(纯内存操作,不发任何网络请求,不需要 GPU 与真实凭据)

1. **实践目标**:直观看到六种配置的字段差异、`repr=False` 对密钥的保护、以及本地配置对路径/设备号的归一化。
2. **操作步骤**:在安装了 pdf-craft 的环境中运行以下脚本(示例代码):

   ```python
   # show_configs.py(示例代码)
   from pdf_craft import (
       DeepSeekOCRLocalConfig, DeepSeekOCR2LocalConfig, UnlimitedOCRLocalConfig,
       DeepSeekOCRVendorConfig, DeepSeekOCR2VendorConfig, UnlimitedOCRVendorConfig,
   )

   local = DeepSeekOCRLocalConfig(models_cache_path="models-cache", enable_devices_numbers=[0, 1])
   print(repr(local))

   deepseek = DeepSeekOCRVendorConfig(
       base_url="https://example.com/v1", api_key="sk-super-secret", model="deepseek-ocr",
   )
   print(repr(deepseek))

   unlimited = UnlimitedOCRVendorConfig(ak="my-ak", sk="my-sk")
   print(repr(unlimited))
   ```

3. **需要观察的现象**:
   - 第一行输出中,`models-cache` 变成了**绝对路径**(形如 `PosixPath('/你运行脚本的目录/models-cache')`),`[0, 1]` 变成了 `(0, 1)`;
   - 第二、三行输出中**完全找不到** `sk-super-secret`、`my-ak`、`my-sk` 的踪影;
   - `temperature`、`top_p` 为 `None` 时仍会出现在 DeepSeek 配置的 repr 中(只有 `repr=False` 的字段才被隐藏)。
4. **预期结果**(路径随运行目录变化,待本地验证):

   ```text
   DeepSeekOCRLocalConfig(models_cache_path=PosixPath('.../models-cache'), local_only=False, enable_devices_numbers=(0, 1))
   DeepSeekOCRVendorConfig(base_url='https://example.com/v1', model='deepseek-ocr', temperature=None, top_p=None, max_tokens=8000, timeout_seconds=180)
   UnlimitedOCRVendorConfig(base_url='https://aip.baidubce.com', poll_interval_seconds=2.0, timeout_seconds=180)
   ```

#### 4.1.5 小练习与答案

**练习 1**:三个本地配置类字段完全相同,为什么还要拆成三个类,而不是一个 `LocalOCRConfig(model="deepseek-ocr2", ...)`?

**答案**:类名本身就是「选择哪个模型」的信息载体。消费侧(见 4.2)用 `isinstance(config, DeepSeekOCR2LocalConfig)` 分派后端;若改用字符串字段,忘填或拼错都不会在构造期报错,只能运行到一半才失败。另外三个独立类型也让类型检查器能把「模型族」纳入检查范围。

**练习 2**:`field(repr=False)` 在这里解决了什么实际问题?

**答案**:dataclass 自动生成的 `repr` 会包含所有字段值。若不做处理,`print(config)`、日志、异常栈中的配置对象都会带出 `api_key`/`ak`/`sk` 明文。`field(repr=False)` 把凭据字段从 repr 中剔除,是一种默认安全的日志卫生习惯。

**练习 3**:`OCRMode` 类型在运行时是什么?它和 `OCRConfig` 有什么关系?

**答案**:运行时 `OCRMode` 就是 `str`(Literal 只在类型检查阶段起作用)。`OCRMode` 是六种模式的**名字**,`OCRConfig` 是六种配置**实体**的联合;仓库 CLI 用前者接收用户输入,再装配出后者的实例。

### 4.2 本地与远程抉择:六个配置如何映射到真实后端

#### 4.2.1 概念说明

配置类只是「愿望」,真正干活的是 `doc-page-extractor` 上游包提供的页面提取器。`ocr_config.py` 里没有任何 `import doc_page_extractor`——**配置与执行是彻底分离的**:本库定义配置的形状,上游包提供运行时。

选择 local 还是 vendor,本质上是一道资源选择题:

| 维度 | local(本地) | vendor(远程) |
| --- | --- | --- |
| 模型跑在哪 | 你的 NVIDIA GPU | 服务提供方的机器 |
| 前置条件 | `pip install 'pdf-craft[local]'` + CUDA | 一组服务凭据 |
| 网络需求 | 首次下载模型后可离线 | 每一页都要联网 |
| 适合谁 | 有 GPU、注重隐私/批量离线处理 | 无 GPU、想开箱即用 |

依赖归属在 [pyproject.toml:48-51](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L48-L51) 写得很清楚:`doc-page-extractor` 本体是**标准依赖**,而 `local = ["doc-page-extractor[local]"]` 扩展补齐的是 transformer/CUDA 相关的 GPU 运行时。所以标准安装的用户可以直接用三种 vendor 配置,想用 local 才需要加装扩展。

#### 4.2.2 核心流程

`PageExtractorNode` 在第一次真正需要识别页面时,才按配置类型创建后端(延迟加载,与上一讲门面的惰性思路一脉相承):

```text
OCR 首次识别页面
    │
    ▼
PageExtractorNode._create_page_extractor()   # 此刻才 import doc_page_extractor
    │
    ├─ isinstance(config, DeepSeekOCRLocalConfig)    → create_deepseek_ocr_page_extractor(ocr_model="deepseek-ocr", ...)
    ├─ isinstance(config, DeepSeekOCR2LocalConfig)   → create_deepseek_ocr_page_extractor(ocr_model="deepseek-ocr2", ...)
    ├─ isinstance(config, UnlimitedOCRLocalConfig)   → create_unlimited_ocr_page_extractor(...)
    ├─ isinstance(config, DeepSeekOCRVendorConfig)   → create_deepseek_ocr_vendor_page_extractor(...)
    ├─ isinstance(config, DeepSeekOCR2VendorConfig)  → create_deepseek_ocr2_vendor_page_extractor(...)
    └─ isinstance(config, UnlimitedOCRVendorConfig)  → create_unlimited_ocr_vendor_page_extractor(...)
    │
    └─ 都不是 → TypeError
```

注意前两个分支共用同一个工厂函数,靠 `ocr_model` 参数区分 `"deepseek-ocr"` 与 `"deepseek-ocr2"`。

#### 4.2.3 源码精读

**(1) 缺少 local 运行时时的友好报错**

[pdf_craft/pdf/page_extractor.py:42-50](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L42-L50) 定义了本地后端的包装工厂:创建本地提取器时若捕获到 `ModuleNotFoundError`(GPU 运行时缺失),就转成一条**可操作**的 `RuntimeError`:

```python
def _create_local_page_extractor(factory):
    """Turn a missing optional local runtime into an actionable package error."""
    try:
        return factory()
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "Local OCR requires the optional local runtime. "
            "Install it with: pip install 'pdf-craft[local]'"
        ) from error
```

用户看到的不是一长串 import 栈,而是一句「装这个包就能解决」。这是可选依赖处理的经典范式。

**(2) isinstance 分派链**

[pdf_craft/pdf/page_extractor.py:63-99](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L63-L99) 处理三种本地配置:逐个 `isinstance` 判断,在分支内部才 `from doc_page_extractor.extractor import ...`(函数内 import = 延迟加载),再把配置字段原样转发给工厂。例如 DeepSeek OCR 2 分支把 `models_cache_path`、`local_only`、`enable_devices_numbers` 一一透传,并指定 `ocr_model="deepseek-ocr2"`。

远程配置的处理在 [pdf_craft/pdf/page_extractor.py:100-155](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L100-L155):DeepSeek 两个 vendor 配置被逐字段复制成上游的配置对象([L100-L137](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L100-L137)),Unlimited vendor 同样复制 `ak`/`sk`/`base_url`/`poll_interval_seconds`/`timeout_seconds`([L138-L154](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L138-L154));六个分支都不匹配则抛 `TypeError`([L155](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L155))。这段「逐字段复制」看起来重复,但换来的是:**本库的配置对象不与上游类耦合**,上游改内部实现时只需调整这一处转换。

**(3) 一个提前暴露的校验**

[pdf_craft/pdf/page_extractor.py:336-341](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L336-L341) 在识别每页前校验 `ocr_size`:DeepSeek OCR 2 的本地路径配 `tiny` 尺寸会被直接拒绝。官方文档 [docs/en/OCR_BACKENDS.md:33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/OCR_BACKENDS.md#L33) 也说明了该组合未经验证。注意 `ocr_size` 属于 `ExtractionOptions`(下一讲主角),不在 OCR 配置里——这里提前剧透了两讲之间的分工:**「用哪个后端」归配置类,「这次提取怎么跑」归提取选项**。

#### 4.2.4 代码实践

**实践:亲手触发「缺 local 运行时」的友好报错**

1. **实践目标**:在不安装 `local` 扩展的标准环境中,观察 4.2.3 中那条可操作的 `RuntimeError`,理解可选依赖的失败方式。
2. **操作步骤**(示例代码):

   ```python
   # no_local.py(示例代码)
   from pdf_craft import predownload_models

   predownload_models(models_cache_path="models-cache")
   ```

   `predownload_models` 定义在 [pdf_craft/functions.py:7-18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/functions.py#L7-L18),用于预下载本地模型;它内部会走到创建本地页面提取器这一步。
3. **需要观察的现象**:在未安装 `pdf-craft[local]` 的环境中,应抛出 `RuntimeError`,消息中包含 `pip install 'pdf-craft[local]'`。
4. **预期结果**:抛出含安装指引的 `RuntimeError`。⚠️ 两个注意点(待本地验证):若环境**已安装** local 扩展,此脚本会开始真实下载模型权重(体积较大,注意流量与磁盘);另外该函数即使失败也不应影响你继续使用 vendor 配置——换用 `predownload_models(ocr=DeepSeekOCRVendorConfig(...))` 时行为完全不同(vendor 无模型可下载)。

#### 4.2.5 小练习与答案

**练习 1**:你的笔记本没有 NVIDIA GPU,但想立刻把一份扫描 PDF 转成 Markdown,该选哪些配置?

**答案**:三种 vendor 配置之一。若使用 DeepSeek 系服务,构造 `DeepSeekOCRVendorConfig(base_url=..., api_key=..., model=...)`;若使用百度 Unlimited OCR,构造 `UnlimitedOCRVendorConfig(ak=..., sk=...)`。本地配置需要 `pdf-craft[local]` 扩展与 CUDA GPU,不适合此场景。

**练习 2**:`_create_local_page_extractor` 为什么要捕获 `ModuleNotFoundError` 并重新抛 `RuntimeError`?

**答案**:直接抛出的 `ModuleNotFoundError` 只会指出某个内部模块缺失,用户不知道该装什么。转换后的消息直接给出安装命令 `pip install 'pdf-craft[local]'`,把「出了错」变成「知道怎么修」。

**练习 3**:为什么 DeepSeek OCR 与 DeepSeek OCR 2 的本地配置共用一个工厂函数,而 Unlimited OCR 用另一个?

**答案**:DeepSeek 两代模型在上游 `doc-page-extractor` 中共用同一套加载入口,以 `ocr_model` 参数区分;Unlimited OCR 来自百度,是另一套独立的模型与加载逻辑,所以有专属工厂 `create_unlimited_ocr_page_extractor`。

### 4.3 配置校验:ensure_ocr_config 的兜底与互斥

#### 4.3.1 概念说明

回看 `PDFOptions`([pdf_craft/craft.py:38-45](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L38-L45)):

```python
@dataclass(frozen=True)
class PDFOptions:
    ocr: OCRConfig | None = None
    pdf_handler: PDFHandler | None = None
    models_cache_path: PathLike | str | None = None
    local_only: bool = False
```

注意:除了 `ocr`,还有 `models_cache_path` 和 `local_only` 两个**便捷字段**——它们恰好就是本地配置的前两个字段。也就是说,用户有两种方式表达 OCR 意图:

- **显式式**:`PDFOptions(ocr=任意六种配置之一)`——最通用;
- **便捷式**:`PDFOptions(models_cache_path="...", local_only=True)`——省去构造 `DeepSeekOCRLocalConfig` 的一层包装,只针对默认的本地 DeepSeek OCR。

这就带来一个歧义:如果用户**同时**给了 `ocr=DeepSeekOCRVendorConfig(...)` 和 `models_cache_path="..."`,后者算谁的?算 vendor 配置显然不对(vendor 不需要模型缓存),忽略它又会让用户误以为生效了。`ensure_ocr_config` 用一条规则终结争议:**显式配置与便捷字段互斥,同时出现即抛 `ValueError`**。官方文档 [docs/en/OCR_BACKENDS.md:57-59](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/OCR_BACKENDS.md#L57-L59) 明确说明了这条约定。

#### 4.3.2 核心流程

`ensure_ocr_config` 的全部逻辑可以用一棵决策树表达:

```text
ensure_ocr_config(ocr, models_cache_path, local_only)
    │
    ├─ ocr 不为 None(用户显式给了配置)
    │     ├─ models_cache_path 或 local_only 也给了 → 抛 ValueError(互斥)
    │     └─ 否则 → 原样返回 ocr
    │
    └─ ocr 为 None(用户没给配置)
          └─ 返回 DeepSeekOCRLocalConfig(models_cache_path=..., local_only=...)
             (便捷字段被吸收进默认的本地 DeepSeek 配置 → 兜底)
```

它在两处被调用,且时机都在「真正需要 OCR」的时刻:

1. **提取引擎组装时**:[pdf_craft/transform.py:31-34](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L31-L34) 中 `PDFExtractionEngine.__init__` 把 `ocr`、`models_cache_path`、`local_only` 三个参数交给 `ensure_ocr_config` 归一化,再传给 `OCR`。而引擎本身又是由门面在首次提取时惰性创建的(上一讲讲过的 `_pdf_engine`,[pdf_craft/craft.py:253-262](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L253-L262))。
2. **预下载模型时**:[pdf_craft/functions.py:7-18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/functions.py#L7-L18) 的 `predownload_models` 同样先经 `ensure_ocr_config` 归一化再构造 `OCR`。

完整的调用链是:

```text
PDFCraft(pdf=PDFOptions(ocr=..., models_cache_path=..., local_only=...))
    │  首次 extract_pdf / convert_pdf_to_* 时
    ▼
PDFCraft._pdf_engine()          # craft.py L253-262,惰性组装
    │
    ▼
PDFExtractionEngine(...)        # transform.py L31-34
    │  ensure_ocr_config(ocr, models_cache_path, local_only)
    ▼
OCR(ocr=<归一化后的 OCRConfig>)  # 此后库内部只见单一 ocr 配置
```

归一化的价值:`OCR` 及其下游(4.2 的分派链)**永远拿到一个非 None 的 `OCRConfig`**,不必再关心用户当初用显式式还是便捷式表达意图——两种入口在边界处被收敛成一种形态。

#### 4.3.3 源码精读

函数本体非常短,[pdf_craft/ocr_config.py:132-146](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/ocr_config.py#L132-L146):

```python
def ensure_ocr_config(
    ocr: OCRConfig | None,
    models_cache_path: PathLike | str | None,
    local_only: bool,
) -> OCRConfig:
    if ocr is not None:
        if models_cache_path is not None or local_only:
            raise ValueError(
                "ocr cannot be combined with models_cache_path or local_only."
            )
        return ocr
    return DeepSeekOCRLocalConfig(
        models_cache_path=models_cache_path,
        local_only=local_only,
    )
```

逐行读:

- 前四行是**互斥校验**:`ocr` 已给出时,便捷字段任何一个「有值」(`models_cache_path is not None` 或 `local_only` 为真)都算冲突,立刻抛 `ValueError`,错误消息直接说清楚哪两类参数不能同时出现。
- `return ocr`:显式配置原样通过,不做任何加工。
- 最后三行是**兜底**:没有显式配置时,把便捷字段打包成默认的 `DeepSeekOCRLocalConfig` 返回。注意兜底目标**不是某个远程服务**而是本地 DeepSeek OCR——因此标准安装(未装 local 扩展)的用户如果什么都不传,首次提取时会撞上 4.2 实践里那条 `RuntimeError`。这是初学者最常见的坑:**装完库直接跑,必须显式传一个 vendor 配置,或装好 local 扩展**。

#### 4.3.4 代码实践

**实践:验证互斥校验与兜底行为**(纯内存操作,可反复运行)

1. **实践目标**:亲手触发 `ValueError`,并确认兜底分支生成的配置类型与字段。
2. **操作步骤**(示例代码):

   ```python
   # ensure_check.py(示例代码)
   from pathlib import Path
   from pdf_craft import DeepSeekOCRVendorConfig
   from pdf_craft.ocr_config import ensure_ocr_config

   vendor = DeepSeekOCRVendorConfig(
       base_url="https://example.com/v1", api_key="sk-demo", model="deepseek-ocr",
   )

   # 场景一:显式配置 + 便捷字段 → 应当报错
   try:
       ensure_ocr_config(vendor, "models-cache", False)
   except ValueError as error:
       print("场景一 ValueError:", error)

   # 场景二:显式配置 + 便捷字段为默认值 → 原样返回
   same = ensure_ocr_config(vendor, None, False)
   print("场景二 原样返回:", same is vendor)

   # 场景三:什么都不给 → 兜底为本地 DeepSeek 配置
   fallback = ensure_ocr_config(None, "models-cache", True)
   print("场景三 兜底配置:", repr(fallback))
   print("models_cache_path 已归一化为绝对路径:",
         isinstance(fallback.models_cache_path, Path))
   ```

   说明:`ensure_ocr_config` 未从包顶层导出,需从 `pdf_craft.ocr_config` 模块导入;六个配置类与别名则可从 `pdf_craft` 顶层直接导入。
3. **需要观察的现象**:场景一打印出的错误消息原文;场景二打印 `True`(返回的是同一个对象,frozen 配置无需复制);场景三打印出的类名是 `DeepSeekOCRLocalConfig`,且 `local_only=True`。
4. **预期结果**(待本地验证):

   ```text
   场景一 ValueError: ocr cannot be combined with models_cache_path or local_only.
   场景二 原样返回: True
   场景三 兜底配置: DeepSeekOCRLocalConfig(models_cache_path=PosixPath('.../models-cache'), local_only=True, enable_devices_numbers=None)
   models_cache_path 已归一化为绝对路径: True
   ```

#### 4.3.5 小练习与答案

**练习 1**:`PDFOptions()` 一个参数都不传,最终 OCR 会用什么配置?会发生什么?

**答案**:`PDFOptions.ocr` 为 `None`、便捷字段为默认值,`ensure_ocr_config` 兜底返回 `DeepSeekOCRLocalConfig(models_cache_path=None, local_only=False)`。首次提取时会尝试创建本地 DeepSeek OCR 后端:若未安装 `pdf-craft[local]` 扩展,得到 4.2 实践中的 `RuntimeError`;若已安装,则可能开始下载模型权重。总之**默认不是远程服务**,标准安装的用户应显式传入一种 vendor 配置。

**练习 2**:为什么 `ensure_ocr_config` 在 `ocr` 已给出时要**拒绝**而不是**忽略**便捷字段?

**答案**:静默忽略会让用户以为 `models_cache_path` 生效了,实际却没有——这种「配置看起来对但行为不对」的问题极难排查。立即抛 `ValueError` 把歧义在入口处暴露,失败得越早,排查成本越低。

**练习 3**:库内部(如 `OCR` 类)为什么可以假定拿到的 `ocr` 一定不是 `None`?

**答案**:因为 `PDFExtractionEngine` 与 `predownload_models` 都先经 `ensure_ocr_config` 归一化,`None` 与便捷字段在边界处已被收敛成一个具体的 `OCRConfig` 实例。下游代码因此无需重复判空,这是「在边界做一次校验,内部保持简单」的分层思路。

## 5. 综合实践

把本讲三个模块串起来,写一个 **OCR 配置工厂函数**:输入 `OCRMode` 字符串,输出对应配置对象——这正是在为仓库 CLI(`pdf_craft_tool` 用环境变量装配配置)的做法做一次迷你复刻,也是理解「六选一」映射的最好练习。

**任务**:

1. **实践目标**:实现 `configure(mode: OCRMode) -> OCRConfig`,能根据六种模式字符串构造对应配置并打印关键字段;同时用 `ensure_ocr_config` 验证互斥校验。
2. **操作步骤**(示例代码):

   ```python
   # configure_ocr.py(示例代码)
   from pdf_craft import (
       OCRConfig, OCRMode,
       DeepSeekOCRLocalConfig, DeepSeekOCR2LocalConfig, UnlimitedOCRLocalConfig,
       DeepSeekOCRVendorConfig, DeepSeekOCR2VendorConfig, UnlimitedOCRVendorConfig,
   )
   from pdf_craft.ocr_config import ensure_ocr_config

   LOCAL_TYPES = {
       "deepseek-ocr-local": DeepSeekOCRLocalConfig,
       "deepseek-ocr2-local": DeepSeekOCR2LocalConfig,
       "unlimited-ocr-local": UnlimitedOCRLocalConfig,
   }

   def configure(mode: OCRMode) -> OCRConfig:
       """按六种 OCRMode 字符串返回对应配置对象。"""
       if mode in LOCAL_TYPES:
           return LOCAL_TYPES[mode](models_cache_path="models-cache")
       if mode == "deepseek-ocr-vendor":
           return DeepSeekOCRVendorConfig(
               base_url="https://example.com/v1", api_key="sk-demo", model="deepseek-ocr")
       if mode == "deepseek-ocr2-vendor":
           return DeepSeekOCR2VendorConfig(
               base_url="https://example.com/v1", api_key="sk-demo", model="deepseek-ocr2")
       if mode == "unlimited-ocr-vendor":
           return UnlimitedOCRVendorConfig(ak="demo-ak", sk="demo-sk")
       raise ValueError(f"unknown OCR mode: {mode}")

   if __name__ == "__main__":
       for mode in ("deepseek-ocr-local", "deepseek-ocr2-local", "unlimited-ocr-local",
                    "deepseek-ocr-vendor", "deepseek-ocr2-vendor", "unlimited-ocr-vendor"):
           config = configure(mode)
           print(f"{mode:>22} -> {config!r}")

       print()
       # 互斥校验:显式配置 + 便捷字段 → ValueError
       try:
           ensure_ocr_config(configure("deepseek-ocr-vendor"), "models-cache", False)
       except ValueError as error:
           print("互斥校验通过,错误消息:", error)

       # 兜底:未给配置 → 本地 DeepSeek
       print("兜底配置:", repr(ensure_ocr_config(None, None, False)))
   ```

3. **需要观察的现象**:
   - 六行配置打印:三个 local 的 `models_cache_path` 都是**同一个绝对路径**;三个 vendor 的 repr 中**不出现任何凭据**;
   - 互斥校验按预期抛错;
   - 兜底配置是 `DeepSeekOCRLocalConfig(models_cache_path=None, local_only=False, enable_devices_numbers=None)`。
4. **预期结果**:程序正常结束,输出六种配置与两条校验结论(具体文本待本地验证)。
5. **进阶(可选)**:把 `configure` 的返回值真正塞进 `PDFOptions(ocr=...)`,对照 4.2.2 的分派链,在纸上写出每种 mode 会命中 `page_extractor.py` 的哪个分支——为第三单元「PDF 提取主链路」热身。

## 6. 本讲小结

- OCR 配置共 **3 族模型 × 2 种运行位置 = 6 个 frozen dataclass**:local 三兄弟共享 `models_cache_path` / `local_only` / `enable_devices_numbers`;DeepSeek 双 vendor 走 OpenAI 兼容三件套;Unlimited vendor 走百度 `ak`/`sk` 并带轮询间隔。
- 本地配置的**自定义 `__init__`** 用 `object.__setattr__` 绕过冻结限制,顺手完成 `str → 绝对 Path`、可迭代 → `tuple` 的参数归一化;所有凭据字段用 `field(repr=False)` 防止打印泄漏。
- 配置与执行彻底分离:消费侧 `PageExtractorNode` 用 **isinstance 链**把六种配置映射到 `doc-page-extractor` 的工厂函数,并延迟 import;local 运行时缺失时会给出「`pip install 'pdf-craft[local]'`」的可操作提示。
- `ensure_ocr_config` 在两个入口(提取引擎、`predownload_models`)承担**归一化**职责:显式配置与便捷字段 `models_cache_path`/`local_only` **互斥**,同时给出即抛 `ValueError`;都没给则**兜底为本地 DeepSeek OCR**(不是远程服务——标准安装用户必须显式传 vendor 配置)。
- 归一化之后,库内部永远面对一个非 `None` 的 `OCRConfig`,下游无需判空——「在边界校验一次,内部保持简单」。

## 7. 下一步学习建议

本讲解决了「OCR 配置长什么样、怎么选」。顺着配置体系往下走:

- **下一讲 u2-l2「PDFOptions 与 ExtractionOptions 详解」**:`ocr` 只是 `PDFOptions` 四个字段之一;`ExtractionOptions` 中的 `ocr_size`(本讲 4.2.3 已提前照面)、页面范围、token 上限、回调与中断控制将在那里展开。
- **再下一讲 u2-l3** 会介绍 LLM 配置(`pdf_craft.llm.LLM`),与本讲的 OCR 配置对照着看,能加深「认字与翻译是两套独立配置」的理解。
- 想提前看配置的消费侧全貌,可通读 [pdf_craft/pdf/page_extractor.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py)——它在第三单元 u3-l4「页面提取后端」会作为主角精读。
