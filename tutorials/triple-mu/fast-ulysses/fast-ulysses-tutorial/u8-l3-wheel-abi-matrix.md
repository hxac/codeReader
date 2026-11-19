# wheel 矩阵与 ABI 匹配

## 1. 本讲目标

u8-l2 讲了「一个 wheel 是怎么被正确构建出来的」，u1-l3 讲了「作为用户应该敲哪条命令」。本讲补上中间那一层工程：一次 release 发布的 36 个 wheel 如何被组织成一张机器可读的矩阵、为什么裸 `pip install fast-ulysses` 会在**任何** torch 上都「成功」却装错行、`fast-ulysses wheel-url` 如何替你在四根轴上挑行，以及当扩展加载失败时 `_diagnose` 如何把动态链接器的原始报错翻译成「哪根轴错了」。学完本讲，你应该能够：

1. 复述 [wheel_matrix.json](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/wheel_matrix.json) 的行结构，并推导「36 个 wheel」这个数字。
2. 解释 PEP 440 local version 的排序规则如何让 `pip install fast-ulysses` 解析到最高标签、进而静默把你的 torch 换成 2.13。
3. 按 **machine → python → torch minor → CUDA major** 的顺序复述 `_match` 的匹配算法，并说出每个失败分支会打印什么。
4. 对一次 `import fast_ulysses` 失败，用 `_diagnose` 的分类框架给出归因：torch 本体、torch minor ABI、CUDA major，还是「说不出」。

## 2. 前置知识

本讲不需要 GPU，但需要几块拼图：

- **PEP 440 版本号与 local version**：`0.2.0+torch210cu128` 中 `+` 之后的部分叫**局部版本（local version / 本地标签）**。两条规则是本讲的火药引线：(a) 局部版本参与排序——同一公开版本下，带标签的高于不带的，标签之间逐段比较；(b) **如果版本说明符（specifier）自己不带标签，比较时完全忽略候选的标签**——`==0.2.0` 因此同时匹配 `0.2.0` 和全部 8 个 `0.2.0+torch…`。
- **ABI（应用二进制接口）与 name mangling**：u8-l2 讲过 `_C.so` 里嵌着 `c10d::Work` 虚表、pybind11 模块和 `TORCH_LIBRARY` 注册，C++ 编译器把这些签名编码成 `_ZN3c10…` 这样的符号名。torch 换 minor 时这些 mangled 名字会变，于是旧 `.so` 里的引用变成**悬空的 `undefined symbol`**。
- **动态链接词汇**：加载 `.so` 时，动态链接器 `ld.so` 按 `DT_NEEDED` 条目（soname，如 `libcudart.so.12`）找依赖库；找不到 soname 报 `cannot open shared object file`，符号缺失报 `undefined symbol`。这两种报错**都不说**是 torch 轴还是 CUDA 轴错了——这正是 `_diagnose` 存在的理由。
- **wheel 文件名的 tag**：`fast_ulysses-0.2.0+torch210cu128-cp312-cp312-manylinux_2_28_x86_64.whl` 中，`+torch210cu128` 是版本的一部分（PEP 440 local version），`cp312` 是解释器 tag，`manylinux_2_28_x86_64` 是平台 tag。
- **`_build_meta.py`**：u8-l2 讲过 setup.py 在构建时把它写在扩展旁边，记录 `TORCH_ABI`、`CUDA_VERSION`、`WHEEL_TAG` 等事实——本讲里它是 `_diagnose` 的「built 侧」数据源。
- 承接 u1-l3：wheel 专用索引、`--no-build-isolation`、`doctor` 的用法已讲过，本讲钻进它们的**实现与论证**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [wheel_matrix.json](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/wheel_matrix.json) | 发布面快照：8 行 × (torch minor, CUDA major) + 全局字段，`wheel-url` 的唯一数据源 |
| [cli.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py) | `fast-ulysses` 命令：`doctor` 与 `wheel-url`；`_match` 是四轴匹配器，`_tag` 是标签推导器 |
| [_diagnose.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/_diagnose.py) | 导入失败归因：build 事实（`_build_meta.py`）vs 现场环境（`_live`）的对照表 |
| [__init__.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/__init__.py) | 失败入口：捕获 `_C` 的 ImportError，先经 `explain` 包装再抛出 |
| [setup.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py) | 事实的来源：`_requires()` 生成 `torch==<minor>.*` 钉住，`_write_build_meta` 落盘构建事实 |
| [check_wheel.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/check_wheel.py) | 发布前自证：标签 ↔ 事实 ↔ 元数据三方一致，保证矩阵声称的每一行为真 |
| [test_wheel_matrix.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_wheel_matrix.py) | 唯一检查 wheel_matrix.json 与 release.yml 矩阵一致的东西 |
| [docs/install.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md) | 用户面论证全文：索引、PEP 440 陷阱、导入失败的特征 |
| [tools/make_index.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/make_index.py) | 生成 PEP 503 静态索引（cu12/cu13/每行一个），把「正确解析」变成索引结构问题 |
| [.github/workflows/release.yml](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/.github/workflows/release.yml) | 矩阵的**源头**：8 行矩阵在这里定义，wheel_matrix.json 必须与它同步 |

一条主线贯穿全讲：**矩阵声称的每一行都要有构建期事实背书（check_wheel、test_wheel_matrix），用户环境的每一根轴都要有工具替他读（wheel-url、_diagnose）**——两头缺一，用户就要靠猜。

## 4. 核心概念与源码讲解

### 4.1 wheel_matrix 行结构：8 行 × 4 个 Python + 4 个无标签行 = 36

#### 4.1.1 概念说明

一次 release 的发布面不是「一个 wheel」，而是一张 **(torch minor × CUDA major)** 的矩阵。为什么恰好是这两根轴？因为 `.so` 链接的东西决定了它能在哪运行：torch **minor** 必须精确匹配（嵌着 `c10d::Work` 虚表、pybind11 模块和 `TORCH_LIBRARY` 注册，都活不过一次 minor 升级），CUDA 只需 **major** 匹配（扩展从 toolkit 只链 `libcudart.so.<major>`，别的什么都不链——u8-l2 与 check_wheel 都断言过这一点）。

`wheel_matrix.json` 就是这张矩阵的机器可读快照，`wheel-url` 子命令读它、`test_wheel_matrix.py` 校验它。它的行还有一个特殊标记：PyPI 拒收带 local version 的包，所以**恰好一行**去掉标签上 PyPI，用 `"pypi": true` 标出。

#### 4.1.2 核心流程

一个 release 的 wheel 总数：

\[ 36 = \underbrace{8_{\text{行}} \times 4_{\text{cp310--cp313}}}_{\text{带标签，走索引/find-links}} + \underbrace{4_{\text{cp}}}_{\text{无标签，上 PyPI}} \]

每行的 local version 标签由 `_tag` 推导：

\[ \texttt{tag}(r) = \texttt{torch}\{M\}\{m\}\,\texttt{cu}\{C\text{ 去掉点}\} \]

例如 `torch=2.10.0, cuda=12.8` → `torch210cu128`。注意两个「只取一部分」：

- 行里的 `torch` 字段写着完整版本（`"2.10.0"`），但匹配与标签只用 `major.minor`；
- `cuda` 字段写着 `12.8`/`12.9`/`13.0`，但匹配只用 major。

行的**顺序**也有语义：`_match` 成功时用 `next(...)` 取第一个满足条件的行，而 PyPI 行被刻意放在最后一行——它同时是排序最高的标签（见 4.2）。

#### 4.1.3 源码精读

矩阵全文只有 16 行：

[python/fast_ulysses/wheel_matrix.json:L1-L16](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/wheel_matrix.json#L1-L16) —— 四个全局字段（`machine`、`platform_tag`、`pythons`、`find_links` 模板）加 8 行 `{torch, cuda}`；最后一行 `{"torch": "2.13.0", "cuda": "13.0", "pypi": true}` 是 PyPI 行。

标签的推导在 cli.py：

[python/fast_ulysses/cli.py:L124-L127](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L124-L127) —— `_tag` 把 `(2.10.0, 12.8)` 拼成 `torch210cu128`，docstring 指出 check_wheel.py 解析它的**逆形式**（从标签反推出事实再核对，见 4.4.3）。

矩阵的源头在 release 工作流，`wheel-url` 打印的命令是否 404 全靠两处同步：

[.github/workflows/release.yml:L110-L117](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/.github/workflows/release.yml#L110-L117) —— 8 行构建矩阵，每行带 `torch`、pytorch 下载索引、CUDA 镜像、`cuda_major` 与自由文本 `tag`；最后一行多一个 `pypi_set: true`。

[test/test_wheel_matrix.py:L52-L62](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_wheel_matrix.py#L52-L62) —— 两个断言：8 个推导标签逐一等于 release.yml 的 `tag:` 列表；PyPI 行恰有一行且是最后一行。它的模块 docstring 说明了为什么必须存在：安装期没有任何东西能分辨「一个命名了真实 wheel 的标签」和「一个不存在 wheel 的标签」——pip 对两者都答 `no matching distribution`，所以一边改了另一边没改，`wheel-url` 就会打印一条 404 的命令，直到有人注意到。

用户面的对应表格在文档里：

[docs/install.md:L17-L38](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L17-L38) —— 「36 wheels: 8 rows × 4 CPythons, plus 4 untagged ones for PyPI」，附 torch/CUDA → 标签的对照表，并点明标签就是 wheel 的 PEP 440 local version。

#### 4.1.4 代码实践

**实践目标**：不看表格、只从 JSON 机械推导全部 8 个标签和 wheel 总数，验证你对行结构的理解。

**操作步骤**（示例代码，读者自己运行）：

```python
# derive_matrix.py —— 从 wheel_matrix.json 推导标签与发布规模
import json, pathlib

m = json.loads(pathlib.Path("python/fast_ulysses/wheel_matrix.json").read_text())
def tag(r):  # 与 cli._tag 同构：torch{M}{m}cu{C 去点}
    mj, mn = r["torch"].split(".")[:2]
    return f"torch{mj}{mn}cu{r['cuda'].replace('.', '')}"

print(len(m["rows"]), "rows;",
      len(m["rows"]) * len(m["pythons"]) + len(m["pythons"]), "wheels per release")
for r in m["rows"]:
    print(f'  {tag(r):16s} pypi={r.get("pypi", False)}')
```

**需要观察的现象**：行数、总数、标签逐一列出，最后一行带 `pypi=True`。

**预期结果**（由文件内容直接决定）：

```text
8 rows; 36 wheels per release
  torch210cu128     pypi=False
  ...
  torch213cu130     pypi=True
```

与 [docs/install.md:L26-L35](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L26-L35) 的表格逐行对照应完全一致。

#### 4.1.5 小练习与答案

**练习 1**：行里 `torch` 写的是 `"2.10.0"`，`cuda` 写的是 `"12.8"`。如果未来发布 torch 2.10.1 对应的行，JSON 需要改吗？
**答案**：不需要新行，只需把 `"torch": "2.10.0"` 的字段值理解为「2.10 这个 ABI」；匹配代码 `_match` 只取 `r["torch"].split(".")[:2]`，patch 号根本不参与。但要注意：字段值是给人看的，若改成 `"2.10.1"` 也不影响匹配——`_tag` 同样只取前两段。

**练习 2**：CUDA 字段有 `12.8`、`12.9`、`13.0` 三种 minor，为什么 torch 2.12 的两行是 `12.9`/`13.0` 而不是 `12.8`/`13.0`？
**答案**：`cuda` 字段记录的是**该行构建时所用 torch 自带的 CUDA 运行时版本**（release.yml 里 torch 2.12.1 从 `whl/cu129` 下载）。它不影响匹配——匹配只用 major；minor 写出来只是为了让标签（`torch212cu129`）与用户从 torch 下载 URL 读到的后缀对得上。

**练习 3**：为什么 PyPI 行只能有一行，且必须去掉 local version？
**答案**：PyPI 拒收带 PEP 440 local version 的上传（见 docs/install.md L120-121），所以恰好一行以无标签形式上 PyPI；它的元数据是 `Requires-Dist: torch==2.13.*`。多于一行会同名同版本冲突，零行则裸 `pip install` 完全找不到包——一行是这个约束下唯一的解，代价见 4.2。

### 4.2 PEP 440 局部版本陷阱：裸 pip install 为什么会静默换 torch

#### 4.2.1 概念说明

三条 PEP 440 规则合谋出一个陷阱：

1. **局部版本参与排序**：\( v < v{+}\ell \)（同公开版本下带标签的高于不带的），标签之间**逐段**比较（数值段按数、字母段按字典序）。
2. **不带标签的 specifier 忽略候选的标签**：`==0.2.0` 匹配 `0.2.0` 和所有 `0.2.0+…`。
3. **pip 在多个匹配中取最高版本**。

于是对任何容纳了多于一行 wheel 的文件集（`--find-links`、`--extra-index-url`、一个装满 wheel 的目录、乃至 PyPI 本身），不带标签的请求都解析到**排序最高的那个标签**——与本机 torch 是什么完全无关。

#### 4.2.2 核心流程

一次 release 的 9 个版本工件（8 带标签 + 1 无标签）的排序链：

\[ \texttt{0.2.0} < \texttt{0.2.0+torch210cu128} < \texttt{0.2.0+torch210cu130} < \texttt{0.2.0+torch211cu128} < \cdots < \texttt{0.2.0+torch213cu130} \]

specifier 语义形式化：

\[ v \in \mathrm{match}(\texttt{==0.2.0}) \iff v_{\text{public}} = \texttt{0.2.0} \quad (\text{标签被忽略}) \]

由此，「裸 `pip install fast-ulysses`」的完整事故链是：

```text
pip install fast-ulysses
  └─ PyPI 只有 torch213cu130（无标签形式），无需选择
     └─ 其元数据 Requires-Dist: torch==2.13.*
        └─ 本机 torch 2.11 不满足 → pip 不会失败，
           而是从 PyPI 装 torch 2.13（不同的 torch、不同的 CUDA 构建）
              └─ 报告为「普通的依赖解析」→ 无任何警告
```

两条续发症：装完后再把 torch 重装回 2.11，就得到 4.4 的 `undefined symbol`；而之后任何 `pip install fast-ulysses` 都打印 `Requirement already satisfied`，**错误的行原地不动**（`--upgrade` 也只会升到最高标签）。唯一的根治是把标签写全：`fast-ulysses==0.2.0+torch210cu128`。

#### 4.2.3 源码精读

排序链与三条对照命令在文档里原样列出：

[docs/install.md:L127-L145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L127-L145) —— 「a specifier without one ignores the label entirely」；同一个 wheelhouse 目录下，不带标签、`==0.2.0`、`==0.2.0+torch210cu128` 三条命令分别装到 `+torch213cu130`、`+torch213cu130`、指定行。

[docs/install.md:L114-L124](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L114-L124) —— PyPI 行为的官方断言：**On any other torch this command does not fail**——pip 用升级 torch 来满足 pin。

[docs/install.md:L154-L157](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L154-L157) —— `Requirement already satisfied` 不替换已安装版本，`--upgrade` 也救不了。

「最高者胜」的能量来自 wheel 自己的元数据，而元数据由构建时生成：

[setup.py:L59-L83](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L59-L83) —— `_requires()` 在构建现场读 `torch.__version__`，产出 `torch==<major>.<minor>.*`；docstring 说明了为什么钉到 minor（`c10d::Work` 虚表、op schema 里的 `c10::intrusive_ptr`、pybind11 模块、`TORCH_LIBRARY` 注册）以及为什么这迫使构建必须 `--no-build-isolation`。

`wheel-url` 把这段论证在每次输出结尾复述给用户：

[python/fast_ulysses/cli.py:L232-L240](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L232-L240) —— 打印命令之后固定附上：最高标签是 `<version>+<PyPI 行标签>`，所以裸 pip 与 `==<version>` 都解析到 torch `<PyPI 行 ABI>` 的 wheel，它会「把本机的 torch 也搬到 `<PyPI 行 ABI>`。Nothing warns.」

绕开陷阱的结构解是索引，其设计理由写在生成器 docstring 里：

[tools/make_index.py:L4-L9](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/make_index.py#L4-L9) —— 「一个装了全部带标签 wheel 的平面列表，会在每台机器上解析到最新的 torch」；两个纠正手段：按 CUDA major 分子索引（这根轴没有任何 wheel 元数据记录，只有 URL 能表达），以及每个 wheel 里的 torch pin——因为专用索引里没有 torch 可装，pip 只能**回溯**到「已装 torch 恰好满足其 pin」的那一行。

#### 4.2.4 代码实践

**实践目标**：用 `packaging`（PEP 440 参考实现，torch 环境里必有）亲手验证排序链与 specifier 忽略标签这两条规则。

**操作步骤**（示例代码）：

```python
# pep440_trap.py —— 验证裸 pip 解析到最高标签的机制
from packaging.version import Version
from packaging.specifiers import SpecifierSet

tags = ["torch210cu128", "torch210cu130", "torch211cu128", "torch211cu130",
        "torch212cu129", "torch212cu130", "torch213cu129", "torch213cu130"]
vs = ["0.2.0"] + [f"0.2.0+{t}" for t in tags]

print("highest:", max(vs, key=Version))
print("==0.2.0 matches:", [v for v in vs if v in SpecifierSet("==0.2.0")])
print("==0.2.0+torch210cu128 matches:",
      [v for v in vs if v in SpecifierSet("==0.2.0+torch210cu128")])
```

**需要观察的现象**：最高者是谁；三种 specifier 各匹配几个候选。

**预期结果**（PEP 440 语义决定）：

```text
highest: 0.2.0+torch213cu130
==0.2.0 matches: 全部 9 个（标签被忽略，pip 取最高 → torch213cu130）
==0.2.0+torch210cu128 matches: 仅 1 个（写全标签才钉得住行）
```

这正是 [docs/install.md:L141-L145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L141-L145) 三条命令的注释所断言的行为。若你的 `packaging` 版本输出与此不符（该库早期对 local version 的 specifier 语义有过修正），以 packaging 当前文档为准——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`uv pip install` 默认 `first-index` 策略（在第一个 carrying 该项目的索引处停下）。为什么写全标签的版本仍然是对的形式？
**答案**：pip 对多个索引取**并集后选最高版本**，uv 默认停在第一个索引——两种策略下「不带标签的请求」的命运都取决于索引布局；而精确版本 `==0.2.0+torch210cu128` 在两种策略下都只有一个候选（docs/install.md L149-152 的论证）。

**练习 2**：专用索引为什么能阻止「静默换 torch」？
**答案**：索引里只有 fast-ulysses、没有 torch（tools/make_index.py docstring：a sub-index carries no torch of its own）。pip 对最高候选 `0.2.0+torch213cu130` 的 `Requires-Dist: torch==2.13.*` 找不到可装的 torch，只能**回溯**（backtrack）到 pin 已被本机 torch 满足的那一行。一旦加上 `--extra-index-url https://pypi.org/simple`，pip 又有 torch 可装，回溯消失，事故复原。

**练习 3**：如果不加任何索引/查找链接、直接在装好 fast-ulysses 的环境里再跑一次裸 `pip install fast-ulysses`，会发生什么？
**答案**：`Requirement already satisfied`——pip 不替换已安装版本；即便 `--upgrade`，目标也是最高标签那一行。所以修复流程必须先 `pip uninstall -y fast-ulysses`（docs/install.md L209-213 把 uninstall 标注为 not optional），或用写全标签的版本一步替换。

### 4.3 wheel-url 匹配顺序：machine → python → torch minor → CUDA major

#### 4.3.1 概念说明

`wheel-url` 回答「当前环境该装哪个 wheel」。四根轴的**检查顺序不是随意的**：`_match` 的 docstring 说得很清楚——按「各轴被谁决定」排序：machine 与 python 是 **wheel 自身的 tag**（auditwheel/setuptools 决定，装完就不可变），先查；torch minor 与 CUDA major 是 **`.so` 钉住的轴**，需要现场读 torch 才能查。

两个容易忽略的精度选择：

- **CUDA 只比 major**：扩展从 toolkit 只链 `libcudart.so.<major>`，check_wheel.py 断言的也恰是这么多——所以 `12.8` 与 `12.9` 的环境装同一个 `cu128`/`cu129` 行（按各自 major）。
- **torch minor 按整数比较**：`_abi` 把 `"2.9"` 转成 `(2, 9)`、`"2.10"` 转成 `(2, 10)`。字符串排序会给出 `"2.9" > "2.10"` 的假象——这与 PEP 440 对公开版本的数值语义是同一个陷阱。

#### 4.3.2 核心流程

`_match` 的完整判定序（伪代码）：

```text
_match(matrix, live, machine):
  1 machine  ≠ matrix.machine      → "no wheel for {machine}…"           + 源码构建
  2 python   ∉ matrix.pythons      → "no wheel for {python}…"            + 换解释器/源码构建
  3 torch 不可导入（"unimportable"）→ "torch does not import here…"       + 先装 torch
  4 torch_abi ∉ 发布的 minor 集合   → "no wheel for torch {abi}…"
        │  附「.so 嵌 c10d::Work 虚表/pybind11/TORCH_LIBRARY」的解释
        └ 选项：pip install "torch=={最近的发布 minor}.*" 再跑一次，或源码构建
  5 same = 同 minor 的行; majors = 它们的 CUDA major 集合
    live.cuda ∈ {None, unknown}    → CPU/ROCm torch，每个 wheel 都链 libcudart
    live.cuda.major ∉ majors       → "no wheel for CUDA {cuda}…
                                      A CUDA major mismatch fails at import on
                                      libcudart, and no LD_LIBRARY_PATH fixes it."
  6 返回 same 中 CUDA major 相同的那一行（next(...)）
```

第 4 步的「最近建议」逻辑：live 的 minor 低于最低发布值时建议最低那档，否则（高于最高档）建议最高那档。

#### 4.3.3 源码精读

匹配器本体与它的顺序论证：

[python/fast_ulysses/cli.py:L130-L137](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L130-L137) —— docstring：轴按「被决定的顺序」检查；**只匹配 CUDA major 是刻意为之**，因为扩展只链 `libcudart.so.<major>`。

前三道闸门：

[python/fast_ulysses/cli.py:L139-L154](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L139-L154) —— machine 不符直接出局（所有发布 wheel 都是同一个平台 tag）；python 不符建议换解释器；torch 导不了则明说「minor 和 CUDA major 是挑 wheel 的依据，先把要跑的 torch 装上」。

torch ABI 轴与整数排序：

[python/fast_ulysses/cli.py:L118-L121](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L118-L121) —— `_abi` 转整数对，`2.9 sorts below 2.10`。

[python/fast_ulysses/cli.py:L156-L166](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L156-L166) —— 把全部行的 minor 收集排序后比对；失败消息里嵌着 ABI 论证（「.so 嵌着 `c10d::Work` 虚表、pybind11 模块和 `TORCH_LIBRARY` 注册，没有一个能活过 torch minor 升级」）加一个可执行的最近建议。

CUDA 轴：

[python/fast_ulysses/cli.py:L168-L184](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L168-L184) —— `torch.version.cuda` 为 `None` 即 CPU/ROCm 构建；major 不符的消息直接预告了失败模式（导入时挂在 libcudart 上，`LD_LIBRARY_PATH` 救不了）；成功则从同 minor 的行里取 major 相同的第一行。

子命令的外壳与两处刻意设计：

[python/fast_ulysses/cli.py:L108-L115](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L108-L115) —— `_FROM_SOURCE` 兜底常量：`--no-binary` 不是可选项（否则 pip 拿无标签 PyPI wheel 并把 torch 搬到 2.13），每条失败消息都以这行收尾。

[python/fast_ulysses/cli.py:L187-L231](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L187-L231) —— `_wheel_url`：以文件方式运行时走平名导入回退（torch 导不了时 `import fast_ulysses` 进不来）；基础版本从 `_build_meta` 的 `VERSION` 截掉标签取得（一次 release 每行各发一个 wheel，行只在本次 release 内命名文件）；打印的命令用 `--find-links` 指向 release 的 `expanded_assets` 而非拼资产 URL——文件名携带 auditwheel 压缩出的平台 tag（v0.1.0 出过 `manylinux_2_24_x86_64.manylinux_2_28_x86_64`），写死文件名等于猜；release 页面本身不含 `.whl` 链接，`expanded_assets` 是它懒加载资产列表的那个片段，36 个全在其中。

#### 4.3.4 代码实践

**实践目标**：跑通 `wheel-url` 并逐行注释输出的每句话来自哪条分支；再用伪造的 `live` 字典直接调 `_match`，拿到三条失败消息的原文。

**操作步骤**：

1. 在仓库根目录以文件方式运行（不依赖已安装的包；console 脚本 `fast-ulysses wheel-url` 等价，但要求包可导入）：

   ```bash
   python python/fast_ulysses/cli.py wheel-url
   ```

2. 逐行注释输出：`running:` 行来自 `cli.py` L198（`_live()` + `platform.machine()`）；`row:` 行来自 L212-215；若命中 PyPI 行打印 `pip install fast-ulysses==<version>`（L216-223），否则打印 `--find-links` 命令加 PEP 440 警告（L228-240）。
3. 直接驱动匹配器（示例代码）：

   ```python
   # drive_match.py —— 伪造 live 字典，收集各轴的失败消息
   import json, pathlib, sys
   sys.path.insert(0, "python/fast_ulysses")   # 平名导入：绕开会 import _C 的包 __init__
   import cli  # noqa: E402

   matrix = json.loads(pathlib.Path("python/fast_ulysses/wheel_matrix.json").read_text())
   base = {"python": "cp312", "torch": "2.9.1+cu128", "torch_abi": "2.9", "cuda": "12.8"}
   for live, machine in [
       (base, "aarch64"),                                # 轴 1：machine
       (base, "x86_64"),                                 # 轴 4：torch minor
       ({**base, "torch": "2.12.5+cu118", "torch_abi": "2.12", "cuda": "11.8"}, "x86_64"),  # 轴 5
       ({**base, "torch": "2.12.5+cu129", "torch_abi": "2.12", "cuda": "12.9"}, "x86_64"),  # 命中
   ]:
       r = cli._match(matrix, live, machine)
       print("->", r if isinstance(r, str) else f"ROW {r}")
   ```

**需要观察的现象**：四组输入各自的返回；注意第 2 组先过 machine 关、再倒在 torch minor 关，输出并不提 python。

**预期结果**（由 `_match` 的分支决定）：

- aarch64 → `no wheel for aarch64: every published wheel is manylinux_2_28_x86_64.` + 源码构建选项；
- torch 2.9 → `no wheel for torch 2.9: published for torch 2.10, 2.11, 2.12, 2.13.` + ABI 论证 + `pip install "torch==2.10.*"`（低于最低发布档，near 取最低）；
- torch 2.12 + CUDA 11.8 → `no wheel for CUDA 11.8: torch 2.12 is published for CUDA 12 and 13.` + 「major 不符在导入时挂在 libcudart 上」；
- torch 2.12 + CUDA 12.9 → 返回行 `{"torch": "2.12.1", "cuda": "12.9"}`。

第 1 步的真实输出取决于当前环境的 torch 与构建状态：未构建的源码树打印 `running:`（若匹配失败则先打印失败消息）后，止步于 `_build_meta.py is missing, so the release this package came from cannot be named.` 并返回 1——注意 `row:` 行在代码里排在版本检查（L207-210）**之后**，所以未构建时根本不会出现——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 machine 检查在 python 之前、python 检查在 torch 之前？
**答案**：按「被谁决定」排序（docstring L132-134）。machine/python 是 wheel 文件名自带的 tag，与现场 torch 无关，不读 torch 就能排除；torch minor 与 CUDA major 要 `import torch` 现场读取——包括「torch 根本导不了」这种情况本身也是先检后查（L150）。

**练习 2**：torch 2.14（假设高于全部发布行）的环境跑 `wheel-url`，`near` 建议是哪个？为什么不是「最近的」2.13 之外的东西？
**答案**：`near = abis[-1]` 即 2.13——`_abi(live) > _abi(abis[-1])` 时取最高发布档（L158）。建议的语义是「降到已发布的最近一档」，因为高于矩阵的 torch 只能装源码构建（sdist 对着已装 torch 编译），消息末尾的 `_FROM_SOURCE` 就是那条路。

**练习 3**：`wheel-url` 为什么打印 `--find-links <expanded_assets>` 而不是一个直接指向 `.whl` 的 URL？
**答案**：见 cli.py L224-231 的注释——文件名里的平台 tag 由 auditwheel 压缩决定（出现过双 tag），写死文件名是猜；release 页面 HTML 不含 `.whl` 链接、对 pip 无用；`expanded_assets` 是页面懒加载资产列表的片段，列出全部 36 个文件，让 pip 自己按 tag 挑。

### 4.4 导入失败归因：_diagnose 的四分类

#### 4.4.1 概念说明

`.so` 加载失败时，`ld.so` 只会说两种话：`undefined symbol: _ZN3c10…`（某个 mangled 符号找不到）或 `libcudart.so.12: cannot open shared object file`（某个 soname 找不到）。**两种话都不说本项目的两根钉子轴——torch minor、CUDA major——哪根错了**。`_diagnose` 的职责就是补上这个命名：拿两份数据做对照——

- **built 侧**：`_build_meta.py` 里 setup.py 烘进去的构建事实（`TORCH_ABI`、`CUDA_VERSION` 等）；
- **live 侧**：当前环境的 `torch.__version__`、`torch.version.cuda`、解释器 tag。

它只在失败路径上被导入（「working load pays nothing for it」）。

#### 4.4.2 核心流程

`_verdict` 的分支表（按代码顺序）：

| # | 条件 | 归因 | 修复指向 |
| --- | --- | --- | --- |
| 1 | live torch 本身导不了 | 「loader 报错是 torch 自己的」 | 先修/装 torch |
| 2 | built 为空（`_build_meta.py` 缺失） | 「unknown build，无从对照」 | 重新构建或换正规 wheel |
| 3 | 报错含 `undefined symbol` 且 minor 失配 | **torch minor ABI 不匹配** | 装与本 torch 配套的构建 |
| 4 | 报错含 `undefined symbol` 且 minor 一致 | 「缺的符号不是 torch minor 引起的」 | 另找原因 |
| 5 | 报错含 `libcudart` 且 major 失配 | **CUDA major 不匹配** | 装对应 CUDA major 的行 |
| 6 | 报错含 `libcudart` 且 major 一致 | 「cudart 装了但没有地方找得到它」 | 查 torch/nvidia wheel 安装 |
| 7 | 以上皆不中 | 「loader 消息本身就是全部」 | 兜底 |

一个容易忽略的细节：`explain` 报告的**主语**是动态选择的——torch 导不了时报 `torch failed to load`，否则报 `fast_ulysses._C failed to load`。因为 `__init__.py` 里 `import torch` 先于 `import ._C`，把 torch 的失败错怪到 `_C` 头上会把读者引向错误的排查方向。

归因的**前提是 built 侧事实为真**——这正是发布管线里 check_wheel.py 的工作（4.4.3 末条）。

#### 4.4.3 源码精读

失败入口与包装：

[python/fast_ulysses/__init__.py:L3-L10](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/__init__.py#L3-L10) —— 先 `import torch`（在 `_C` dlopen 前把 libtorch 装载好），再 `from . import _C`；任何 ImportError 都先经 `_diagnose.explain` 包装成带归因的消息再 `raise … from exc`。所以 `fast-ulysses doctor` 在扩展坏掉时同样抛这份报告而不是运行（cli.py L26-29 的 `_load` docstring 交代了这条路径的分工）。

built 侧与 live 侧的采集：

[python/fast_ulysses/_diagnose.py:L13-L26](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/_diagnose.py#L13-L26) —— `build_meta()` 先试相对导入再试平名（cli.py 以文件运行时没有包可相对），两败俱空返回 `{}`——对应分支 2。

[python/fast_ulysses/_diagnose.py:L29-L41](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/_diagnose.py#L29-L41) —— `_live()` 导不了 torch 时记 `"unimportable"`/`"unknown"`（torch 导不了本身就是一个候选原因），并给出 `torch_abi`（前两段）与 `cp` tag。

归因主体：

[python/fast_ulysses/_diagnose.py:L44-L70](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/_diagnose.py#L44-L70) —— 分支表 1-7 的实现：`undefined symbol` 比对 `TORCH_ABI`（L52-62），`libcudart` 比对 CUDA **major**（L63-69，与 `_match` 的精度选择一致）。

[python/fast_ulysses/_diagnose.py:L73-L85](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/_diagnose.py#L73-L85) —— `explain` 的输出格式：主语（torch 还是 `_C`）+ 归因句 + `built against:`（烘进去的全部事实）+ `installed now:` + `loader error:`（loader 的原话永远在末尾）。

built 侧事实的产地：

[setup.py:L149-L171](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L149-L171) —— `_write_build_meta` 在扩展旁写出 `VERSION/WHEEL_TAG/TORCH_VERSION/TORCH_ABI/CUDA_VERSION/PYTHON_ABI/CUDA_ARCH_LIST/HAS_WORK_REGISTRY`。docstring 点明时机：`build_info()` 在 dlopen 本身失败时无法应答，而这正是需要这些事实的时刻。

发布前的「事实为真」保证（归因能成立的前提）：

[tools/check_wheel.py:L185-L219](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/check_wheel.py#L185-L219) —— 断言 wheel 的 `Requires-Dist` 确有 `torch=={TORCH_ABI}.*`，且 `_build_meta` 的 CUDA major 与构建容器一致（「cu13 的 torch 在 CUDA 12 镜像里构建会链 libcudart.so.12、然后在 cu13 运行时上死」——下游没有任何东西能发现）。

[tools/check_wheel.py:L222-L252](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/check_wheel.py#L222-L252) —— `_check_wheel_tag` 把文件名里的 local version **反解**回 torch/CUDA 版本，与 `_build_meta` 对照：release.yml 的 `tag:` 是自由文本，若标签写 `torch212cu130` 而实际 torch 是 2.11，其余断言全过、发出去的文件名在撒谎。

用户面的特征对照：

[docs/install.md:L187-L207](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L187-L207) —— 两种典型报错的语义：`undefined symbol: _ZN3c10…` = wheel 的 torch minor 与已装 torch 不符（含「pip 把 torch 搬走后又装回来」的续发症）；`libcudart.so.12: cannot open shared object file` = CUDA-12 wheel 在 CUDA-13 环境（或反之），`LD_LIBRARY_PATH` 修不了。修复顺序：先查 torch 是不是你选的那个，再 uninstall + 重装。

#### 4.4.4 代码实践

**实践目标**：亲手触发一次导入失败并读懂归因；再用伪造的 built/live 直接调 `_verdict`，拿到 torch-minor 与 CUDA-major 两类消息的原文。

**操作步骤**：

1. 在**未构建**的源码树（没有 `_C`、没有 `_build_meta.py`；torch 可导入）上触发失败：

   ```bash
   PYTHONPATH=python python -c "import fast_ulysses"
   ```

2. 平名导入 `_diagnose`（纯 stdlib），伪造两侧数据（示例代码）：

   ```python
   # drive_verdict.py —— 复现两类归因消息
   import sys
   sys.path.insert(0, "python/fast_ulysses")
   import _diagnose  # noqa: E402

   built = {"TORCH_ABI": "2.12", "CUDA_VERSION": "12.9"}
   live = {"torch": "2.13.0+cu130", "torch_abi": "2.13", "cuda": "13.0", "python": "cp312"}
   print(_diagnose._verdict("undefined symbol: _ZN3c10I…", built, live), end="\n---\n")
   print(_diagnose._verdict("libcudart.so.12: cannot open shared object file", built, live))
   ```

**需要观察的现象**：第 1 步的报错不再是裸 ModuleNotFoundError，而是四行结构的报告；第 2 步两条消息分别命中分支 3 与分支 5。

**预期结果**：

- 第 1 步（未构建树 + torch 可导入）：

  ```text
  fast_ulysses._C failed to load: unknown build: _build_meta.py is missing, so there is nothing to compare against.
    built against: unknown build
    installed now: torch=<本机版本>, cuda=<本机 CUDA>, python=cp<XX>
    loader error: No module named 'fast_ulysses._C'
  ```

- 第 2 步第一条（分支 3）：`built against torch 2.12, but torch 2.13 is installed, and a torch minor bump changes the C++ ABI this extension links against. Install a build made for this torch.`
- 第二条（分支 5）：`built for CUDA 12.9, but torch reports CUDA 13.0. Install the build whose CUDA major matches this torch.`

第 1 步的 `installed now:` 一行内容随环境而变，报告骨架与第一行是确定的——**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：装了 `torch210cu128` 行的 wheel，然后单独把 torch 升到 2.13。`import fast_ulysses` 的归因是哪一类？为什么不是 CUDA 类？
**答案**：分支 3——报错含 `undefined symbol`（torch 2.13 里旧 mangled 名字没了），且 `built["TORCH_ABI"]="2.10" ≠ live["torch_abi"]="2.13"`。CUDA 类（分支 5/6）只在报错提到 `libcudart` 时进入；torch 升级不动 soname 的 major，`libcudart` 还找得到。

**练习 2**：为什么 `_verdict` 里 CUDA 比较用 `split(".")[0]`（只比 major），而 torch 比较用完整 minor？
**答案**：两根轴的钉法不同。扩展从 toolkit 只链 `libcudart.so.<major>`（check_wheel 的 REQUIRED_NEEDED 断言），所以 CUDA minor 无关；而 torch 的 C++ ABI 在 minor 边界上变化（vtable、pybind11、TORCH_LIBRARY 注册），minor 必须精确。`_match`（L136-137）与 `_requires()`（setup.py）做的是同一组精度选择——三处必须一致，否则会出现「wheel-url 挑的行与实际能运行的行不同」。

**练习 3**：分支 6（`libcudart` 在报错里、CUDA major 却一致）是什么事故？
**答案**：cudart 本身装了、major 也对，但没有任何搜索路径找得到它——典型的是 torch/nvidia 的 pip wheel 安装残缺或环境被破坏。这已经**不是**矩阵选行问题，`_verdict` 如实说「cudart is installed where nothing finds it」，把排查引向安装面而非发布面。

## 5. 综合实践

把本讲四条线串成一次「发布面巡检 + 事故演练」：

**任务 A：矩阵巡检脚本**。写一个 `audit_matrix.py`（示例代码，读者完成）：

1. 读 `wheel_matrix.json`，对 8 行各伪造一个命中的 `live`（如 torch `2.11.3+cu130`、cuda `13.0`、python `cp311`、machine `x86_64`），断言 `_match` 恰好返回该行；
2. 对四根轴各伪造一个失配（aarch64 / cp39 / torch 2.9 / CUDA 11.8），断言返回的字符串**第一行**分别以 `no wheel for aarch64`、`no wheel for cp39`、`no wheel for torch 2.9`、`no wheel for CUDA 11.8` 开头；
3. 断言 PyPI 行恰为最后一行，且 `_tag(该行)` 是对全部行做 `max(..., key=标签的 PEP 440 排序)` 的胜者——用 4.2 的 `packaging` 手法验证。

**任务 B：三份事故报告**。每份不超过 10 行，写清「现场报错原文（首行）→ `_diagnose` 归因分支 → 修复命令」：

1. 裸 `pip install fast-ulysses` 后 torch 被搬到 2.13（用 `python -c "import torch; print(torch.__version__, torch.version.cuda)"` 取证）；
2. 装了错误行（torch210cu128）后又重装了 torch 2.11——预期 `undefined symbol`，归因分支 3；
3. CUDA-12 行的 wheel 装在 CUDA-13 的 torch 环境里——预期 `libcudart.so.12: cannot open shared object file`，归因分支 5，修复必须含 `pip uninstall -y fast-ulysses`。

判据：报告里的归因句应与 `_diagnose._verdict` 的输出逐字一致（用 4.4.4 的驱动脚本核对），修复命令应与 `wheel-url` 打印的形式一致（写全标签或走索引）。多 GPU 不是本实践的前提；若无可导入的 torch，任务 A 仍可完整运行（它不碰 torch），任务 B 以纸面推演完成并标注「待本地验证」。

## 6. 本讲小结

- 一次 release 是 **36 个 wheel**：8 个 (torch minor × CUDA major) 行 × 4 个 CPython，加 4 个去掉 local version 上 PyPI 的无标签行；`wheel_matrix.json` 是这张矩阵的机器可读快照，`test_wheel_matrix.py` 是它与 release.yml 之间唯一的同步检查。
- PEP 440 的三条规则（局部版本参与排序、无标签的 specifier 忽略标签、pip 取最高）合谋出**静默换 torch**：任何多行文件集上，裸 `pip install fast-ulysses` 都解析到 `+torch213cu130`，再用其 `torch==2.13.*` pin 把你的 torch 搬走——唯一的根治是把标签写全，或走不含 torch 的专用索引让 pip 回溯。
- `wheel-url` 的 `_match` 按「被谁决定」排序四根轴：machine/python（wheel 自身 tag）在前，torch minor/CUDA major（`.so` 钉住的轴）在后；CUDA 只比 major，因为扩展从 toolkit 只链 `libcudart.so.<major>`；每个失败分支都给出可执行选项并统一收尾在源码构建命令上。
- 导入失败时 `ld.so` 的两种话（`undefined symbol` / 缺 `libcudart` soname）都不指认哪根轴；`_diagnose` 用「`_build_meta` 的构建事实 vs `_live` 的现场」对照完成归因，且报告主语动态选择——torch 自己坏了不冤枉 `_C`。
- 归因成立的前提是事实为真：check_wheel.py 在发布前反解文件名标签、核对 `Requires-Dist` 与 `_build_meta`，保证矩阵声称的每一行与实际发的 wheel 一致——错误检查比没有检查更危险。

## 7. 下一步学习建议

下一讲 u9-l1 进入测试体系：四层测试如何分工，以及 `test_wheel_matrix.py` 所在的「host-only 无 GPU 测试层」能为 CI 证明什么、不能证明什么——本讲的矩阵一致性检查正是那一层的一员。继续阅读建议：先精读 [tools/make_index.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/make_index.py) 的完整 docstring（索引三命名的设计论证比本讲引用的两段更细），再对照 [docs/install.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md) 的 "When the import fails" 一节做一次本讲 4.4 的实践，最后带着「发布面如何被验证」的问题进入 u9 单元。
