# eepBinCvt：把 bin 转成裸机可用的 mem/header

## 1. 本讲目标

上一讲（u3-l1）我们用 `eeptpu_compiler` 把 `yolov4_tiny.cfg/.weights` 编译成了 TPU 可执行的 `*.pub.bin`。但如果你直接打开裸机工程（`sdk/standalone`），会发现 `main.cc` 里 `#include` 的是一个叫 `eepnet.h` 的文件，SD 卡上读的也是 `eepnet.mem`、`eepinput.mem`——并没有人直接去读那个 `*.pub.bin`。

`*.pub.bin` 和 `eepnet.h / eepnet.mem / eepinput.mem` 之间，隔着一个叫 **eepBinCvt** 的转换工具，以及一个叫 `eepbin_cvt.sh` 的脚本。本讲就负责打通这一段。

学完本讲你应当能：

- 说清楚 `*.pub.bin` 与 `mem/header` 两类产物在**形态和用途**上的区别。
- 解释裸机工程为什么不能像 Linux demo 那样「直接喂一个 bin 给运行库」，而要先做一次格式转换。
- 读懂 `eepbin_cvt.sh` 里 `--output header` 与 `--output mem` 两条命令各自生成什么。
- 把「cfg/weights → compiler → pub.bin → eepBinCvt → eepnet.h / eepnet.mem / eepinput.mem → 裸机 main.cc」整条链路完整画出来，并写出每一步的命令。
- 理解 `bin_name`（`setting.ini` 里的 `eeptpu_s2.pub.bin`）是如何作为契约，把编译步骤和转换步骤拴在一起的。

## 2. 前置知识

在进入源码前，先用三段话补齐背景。

**第一，什么是 `*.pub.bin`。** 在 u2-l1 里我们说过，TPU bin 不只是权重，它是一个「打包好调度表、地址表、输入输出 shape、mean/norm 预处理系数」的**硬件可直接执行二进制**。`--public_bin`（免费版标志）编译出来的就叫 `*.pub.bin`。Linux 路线下，闭源运行库 `libeeptpu_pub` 提供一个高层类 `EEPTPU`，你只要 `load_bin("xxx.pub.bin")`，运行库内部会自己解析它。

**第二，裸机工程和 Linux demo 的运行环境差异。** Linux demo 跑在板上的 ARM Linux 里，有文件系统、有动态库、有 `fopen`。裸机 standalone 跑在「FSBL 之后、内核之前」的裸 ARM 上，**没有标准文件系统、没有 libc 动态链接、没有 C++ 异常/RTTI**，一切靠 Vitis 编译成单个 ELF 烧进板子。所以裸机工程想要网络数据，只有两条路可选：

1. 在**编译期**把数据变成 C 数组（`unsigned char eepnet_config[] = {...}`），直接 `#include` 进 ELF——这就是 `header` 模式。
2. 在**运行期**从一个能挂载的块设备（SD 卡）把原始字节读到指定物理地址——这就是 `mem` 模式 + `file_read`。

`*.pub.bin` 这两种形态都不直接是，所以需要 eepBinCvt「翻译」一下。

**第三，eepBinCvt 是什么。** 它是 `sdk/standalone/net_model/eepBinCvt/eepBinCvt` 下的一个 **x86-64 静态链接可执行程序**（在开发主机上跑，不是在板上跑）。它读入 `*.pub.bin`（以及一张样例图），按 `--output` 的取值，拆出裸机工程需要的若干文件。工具本身闭源，但它的输入输出契约完全体现在 `eepbin_cvt.sh` 和生成的 `eepnet.h` 注释里，足够我们理解整条链路。

## 3. 本讲源码地图

本讲涉及的文件集中在「编译/转换脚本」和「裸机工程的消费端」两处：

| 文件 | 作用 | 本讲用它来 |
| --- | --- | --- |
| `sdk/standalone/net_model/scripts/eepbin_cvt.sh` | 调用 eepBinCvt，分别生成 header 与 mem 产物 | 解读转换命令与两种输出模式 |
| `sdk/standalone/net_model/scripts/b_yolo4tiny.sh` | 调用 eeptpu_compiler 生成 pub.bin 并归档 | 看清「编译→转换」的衔接点与 bin_name 契约 |
| `sdk/standalone/net_model/scripts/setting.ini` | 编译参数表，含 `bin_name` | 确认 `eeptpu_s2.pub.bin` 这个名字从哪来 |
| `sdk/standalone/net_model/eepBinCvt/eepBinCvt` | 转换工具本体（闭源 x86-64 二进制） | 确认工具存在与运行平台 |
| `sdk/standalone/src/net_data/eepnet.h` | header 模式的产物（`eepnet_config[]` 元数据数组） | 读懂它装了什么、被谁 include |
| `sdk/standalone/src/net_data/eepnet.mem` / `eepinput.mem` | mem 模式的产物（权重 / 输入图） | 看它们在运行期如何被 `file_read` |
| `sdk/standalone/src/main.cc` | 裸机主程序，消费上述产物 | 串联 include → eeptpu_init → file_read |
| `sdk/standalone/src/config.h` | 编译开关与尺寸常量 | 解释 `FG_INPUT_DATA_SEPERATED`、`NET_SIZE` 等 |
| `sdk/standalone/src/eeptpu/eeptpu_sa.cpp` | `eeptpu_init` 解析 `eepnet_config` | 验证 header 产物如何被还原成硬件地址 |

## 4. 核心概念与源码讲解

### 4.1 eepBinCvt 工具作用：为什么裸机需要再做一次格式转换

#### 4.1.1 概念说明

回到那个核心疑问：**Linux 路线一个 `load_bin` 就能用的 `*.pub.bin`，为什么裸机不能直接用？**

答案不在于「bin 里的内容不对」，而在于「裸机工程拿不到/用不上这个 bin 的方式不对」。Linux 运行库 `libeeptpu_pub` 自带一套解析器，能在运行时打开 bin 文件、拆出元数据（输入输出 shape、地址表、mean/norm）、再把权重搬进 TPU 内存。裸机工程没有这套运行时解析器，也不想在板上做一个复杂的文件解析逻辑。所以它把这件事**前置到开发主机上**：

- 让 eepBinCvt 在主机上把 `*.pub.bin` **拆开**：
  - 把「元数据」抽出来，变成一段 C 数组 `eepnet_config[]`（header 模式），编译期就编进 ELF。
  - 把「权重 / 输入数据」抽出来，变成纯字节流 `eepnet.mem` / `eepinput.mem`（mem 模式），运行期从 SD 卡搬进内存。

换句话说，**eepBinCvt 干的是「把一个一体化 bin，拆成裸机工程能 include 和能 file_read 的三件套」**。它不是再编译一次网络，而是一次**格式重排 / 解包**。

#### 4.1.2 核心流程

把本讲放进整条主线里看，从 cfg/weights 到裸机推理一共四步，本讲覆盖第 ③ 步：

```
① eeptpu_compiler  ：yolov4_tiny.cfg/.weights ──► eeptpu_s2.pub.bin   （u3-l1）
② b_yolo4tiny.sh   ：把 pub.bin 归档到 binRoot/yolov4tiny/            （u3-l1）
③ eepBinCvt        ：pub.bin + 样例图 ──► eepnet.h / eepnet.mem / eepinput.mem   （★本讲★）
④ 裸机 main.cc     ：#include eepnet.h + file_read eepnet.mem/eepinput.mem ──► 推理   （u4 起）
```

第 ③ 步内部的两种产物由同一个工具、同一条 bin 输入、不同的 `--output` 开关决定：

```
            ┌── eepBinCvt --output header ──► eepnet.h      （eepnet_config[] 元数据数组）
pub.bin ────┤
            └── eepBinCvt --output mem    ──► eepnet.mem    （权重字节流）
                                          └─ eepinput.mem  （样例图的硬件输入字节流）
```

#### 4.1.3 源码精读

先确认 eepBinCvt 这个工具确实存在、且是主机侧二进制：

[eepBinCvt 目录](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/eepBinCvt/eepBinCvt) —— 它是一个 `ELF 64-bit LSB executable, x86-64, statically linked`，**静态链接**意味着在开发主机上几乎不用装额外依赖就能跑（`file` 命令可验证）。注意：它跑在 x86 主机上，产物再被拷到裸机工程/SD 卡，**不是在板上运行**。

再看转换脚本本体。`eepbin_cvt.sh` 一共就两条实质命令，分别对应 header 和 mem：

[eepbin_cvt.sh:3-6](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/eepbin_cvt.sh#L3-L6) 用 `--output header` 调用 eepBinCvt，输入是上一步编译出的 `eeptpu_s2.pub.bin`，外加一张样例图 `004545.bmp`。

[eepbin_cvt.sh:8-11](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/eepbin_cvt.sh#L8-L11) 用 `--output mem` 再调用一次，**输入完全相同**，只换了输出模式。三次入参的含义：

- `--bin ./scripts/binRoot/yolov4tiny/eeptpu_s2.pub.bin`：要转换的 pub.bin，路径承接 `b_yolo4tiny.sh` 的归档位置（见 4.4）。
- `--input ../models/images/ssd/004545.bmp`：一张样例图，**仅 mem 模式用得上**——eepBinCvt 会把它按网络要求的预处理（mean/norm/定点/通道排布）算成硬件输入字节流，存成 `eepinput.mem`，让裸机在没摄像头时也能跑一张固定图。
- `--output header|mem`：决定生成 header 三件套还是 mem 字节流。

> ⚠️ **路径基准提醒（实测发现）**：这份提交进仓库的 `eepbin_cvt.sh` 里，三条相对路径的基准并不一致——`--bin ./scripts/binRoot/...` 暗示从 `net_model/` 目录运行，而 `../eepBinCvt`、`../models` 又暗示从 `scripts/` 目录运行；脚本末尾还有一个没有配对的 `cd - > /dev/null`（[eepbin_cvt.sh:13](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/eepbin_cvt.sh#L13)），说明开头原本可能有一行 `cd` 被裁掉了。**实际使用时请以你本机工作目录为准，统一调整这三条路径**（最省事的做法是从 `net_model/` 运行，并把 `--bin` 写成 `scripts/binRoot/yolov4tiny/eeptpu_s2.pub.bin`，`--input` 写成 `models/images/ssd/004545.bmp`）。这是阅读型脚本里常见的「需要按环境微调」的坑，下面 4.2.4 会给出一份自洽的示例命令。

#### 4.1.4 代码实践

**实践目标**：用只读手段确认「eepBinCvt 是主机侧工具、pub.bin 是它的输入」这一事实，不实际执行（执行需要先有 pub.bin，见综合实践）。

**操作步骤**：

1. 在仓库根目录运行 `file sdk/standalone/net_model/eepBinCvt/eepBinCvt`，看它的架构与链接方式。
2. 运行 `git ls-files sdk/standalone/src/net_data/`，列出已随仓库提交的产物文件。
3. 打开生成的 `eepnet.h` 第 4 行注释，看是谁生成了它、版本号是多少。

**需要观察的现象**：

- `file` 输出应包含 `x86-64` 与 `statically linked`，证明它在开发主机（不是 ARM 板）上运行。
- `git ls-files` 应列出 `eepnet.h`、`eepnet.mem`、`eepinput.mem` 三个文件——也就是说，**仓库里已经提交了一份转换好的产物**，即使你本机没装编译器/转换工具，也能直接拿这三个文件进 Vitis 跑裸机 demo。
- `eepnet.h` 顶部应写着 `// Generated by eepBinCvt(v2.1.0)`，这正是 eepBinCvt 留下的「出生证明」。

**预期结果**：三步都能对上，说明 eepBinCvt 的角色（主机侧转换器）和它的产物（三件套）得到了源码层面的交叉验证。本步为「源码阅读型实践」，运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：能不能省掉 eepBinCvt，让裸机工程直接 `load_bin("eeptpu_s2.pub.bin")`？

> **答案**：不能。裸机没有 Linux 运行库 `libeeptpu_pub` 那套运行时解析器，也没有标准文件系统来 `fopen` 一个 bin。eepBinCvt 的存在正是为了把「解析 bin」这件事前置到主机编译期/SD 加载期，拆成裸机能 include 和能 `file_read` 的形态。

**练习 2**：eepBinCvt 应该在开发主机上跑，还是在 ZynqMP 板上跑？为什么？

> **答案**：在开发主机上跑。它是 x86-64 二进制（`file` 可证），板上是 ARM；而且转换是一次性的离线步骤，没必要占用板上资源。产物（`.h`/`.mem`）拷到工程/SD 卡即可。

---

### 4.2 header 与 mem 两种输出模式

#### 4.2.1 概念说明

eepBinCvt 最关键的设计就是 **`--output` 的两个取值**，它们对应裸机获取数据的两条路：

| 模式 | 命令 | 产物形态 | 落地方式 | 适合场景 |
| --- | --- | --- | --- | --- |
| `header` | `--output header` | C 源码数组 `eepnet_config[]` | 编译期 `#include` 进 ELF | 体积小、需要编译期可见的**元数据** |
| `mem` | `--output mem` | 纯字节流 `eepnet.mem` / `eepinput.mem` | 运行期 `file_read` 从 SD 搬到内存 | 体积大、可替换的**权重 / 输入数据** |

一条很重要的认知：**header 模式产出的 `eepnet_config[]` 不是权重，而是「元数据表」**——它记录的是输入输出 shape、各段数据在 DDR 里的相对偏移、mean/norm 系数、`bin_type` 等「说明书」。真正的几兆权重，是 mem 模式产出的 `eepnet.mem`。这一点看一眼 `eepnet.h` 的体积（一百多字节）和 `eepnet.mem` 的体积（约 12 MB）就一目了然。

为什么要把元数据和权重拆开？因为：

- **元数据要让 C++ 代码「看见」**：裸机要在运行期据此算出输入分辨率、通道数、输出地址，所以它得是源码里的数组。
- **权重不需要代码看见**：它只是一大坨要原样搬进 TPU 内存的字节，用 `.mem` + `file_read` 最省事，还能不重编 ELF 就换网络（换张 SD 卡即可）。

#### 4.2.2 核心流程

两种模式的产出与去向：

```
eepBinCvt --output header
   └─ eepnet.h        ──► 拷到 sdk/standalone/src/net_data/  ──► main.cc: #include "net_data/eepnet.h"

eepBinCvt --output mem
   ├─ eepnet.mem      ──► 拷到 SD 卡  ──► main.cc: file_read("eepnet.mem",     eepnet,       NET_SIZE)
   └─ eepinput.mem    ──► 拷到 SD 卡  ──► main.cc: file_read("eepinput.mem",   eepinput_addr, INPUTDATA_SIZE)
```

注意 `eepinput.mem` 的特殊性：它是把**一张固定样例图**（`004545.bmp`）预处理成硬件输入格式后的字节流，本质是「让没摄像头的板子也能跑一张图」的测试输入。在带摄像头的实时 demo（`case '5'`）里，输入来自摄像头采集，`eepinput.mem` 就不参与了。

#### 4.2.3 源码精读

`eepbin_cvt.sh` 的两条命令（已在 4.1.3 引用）分别产出 header 和 mem。现在看产物本身。

**header 产物**——`eepnet.h`：

[eepnet.h:1-20](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/net_data/eepnet.h#L1-L20) 是 eepBinCvt `--output header` 生成的文件。关键三处：

- [eepnet.h:4](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/net_data/eepnet.h#L4) 注释 `// Generated by eepBinCvt(v2.1.0)` 和 `// Public bin`——出生证明，且点明源 bin 是 public 类型。
- [eepnet.h:7](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/net_data/eepnet.h#L7) 声明 `unsigned char eepnet_config[] = {...}`——这就是裸机要 include 的全部内容，一个字节数组。
- 数组前 20 个字节里藏着 `bin_type`：按 [eeptpu_sa.cpp:106-115](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L106-L115) 的格式，前 4 个 int 是 `interface/mem_base/tpureg_addr/reg_size`（共 16 字节），第 5 个 int 是 `bin_type`。看 `eepnet.h` 第 9 行开头的 `0x02,0x00,0x00,0x00`——`bin_type=2`，正是 `eeptpu_sa.cpp` 里 `pub=2` 的分支（[eeptpu_sa.cpp:46](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L46) 注释 `bin_type = 1; // enc=1; pub=2`）。这与 `setting.ini` 里 `--public_bin` 一一对应，是「编译参数 → 元数据字段 → 解析分支」贯通的证据。

> 字段级逐项解码（shape、exp、mean/norm 怎么读）是下一讲 u3-l3 的主题，本讲只确认「header 模式产出的是元数据数组，且被 `eeptpu_init` 消费」即可。

**mem 产物**——`eepnet.mem` / `eepinput.mem`：这两个是纯二进制，无可读源码。它们的「大小」被写死在 `config.h` 里作为契约：

[config.h:46-47](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L46-L47) 定义 `NET_SIZE 12240064`（约 11.7 MB，权重）和 `INPUTDATA_SIZE 5537792`（约 5.3 MB，一张 416×416×3 输入按 32 字节步长打包后的体积量级）。`file_read` 就按这两个长度从 SD 卡读，因此**换网络后这两个常量必须跟着新生成的 `.mem` 大小更新**，否则会读爆或读漏。

#### 4.2.4 代码实践

**实践目标**：在不实际执行 eepBinCvt 的前提下，把 `eepbin_cvt.sh` 里两条相对路径不自洽的命令，改写成一份**自洽的示例命令**，便于读者在自己机器上复现。

**操作步骤**：

1. 假设当前工作目录是 `sdk/standalone/net_model/`（即 `scripts/` 的上一层）。
2. 把 4.1.3 给出的两条命令改写为统一以 `net_model/` 为基准的形式（**示例代码**，非仓库原有脚本原样内容）：

```bash
# 示例代码：以 net_model/ 为工作目录的自洽写法（实际路径请按本机调整）
../eepBinCvt/eepBinCvt 不会成立，应改为：
eepBinCvt/eepBinCvt \
    --bin scripts/binRoot/yolov4tiny/eeptpu_s2.pub.bin \
    --input models/images/ssd/004545.bmp \
    --output header

eepBinCvt/eepBinCvt \
    --bin scripts/binRoot/yolov4tiny/eeptpu_s2.pub.bin \
    --input models/images/ssd/004545.bmp \
    --output mem
```

3. 把生成的 `eepnet.h` 拷到 `sdk/standalone/src/net_data/`，把 `eepnet.mem`、`eepinput.mem` 拷到将要挂载的 SD 卡分区。

**需要观察的现象**：

- `--output header` 应在当前目录生成 `eepnet.h`，其顶部注释为 `Generated by eepBinCvt(vX.X.X)`，与仓库里 [eepnet.h:4](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/net_data/eepnet.h#L4) 同款。
- `--output mem` 应生成 `eepnet.mem`（数兆级）与 `eepinput.mem`，文件大小应与 `config.h` 的 `NET_SIZE`/`INPUTDATA_SIZE` 量级吻合。

**预期结果**：能复现仓库里 `net_data/` 已提交的三件套。本步需要先有 `pub.bin`（由 u3-l1 的编译步骤产出），完整可运行链路见第 5 节综合实践；若本机无 eeptpu_compiler/eepBinCvt 运行环境，则**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么元数据走 header、权重走 mem，而不是反过来？

> **答案**：元数据需要被 C++ 代码在编译期/运行期直接读取（算 shape、地址），所以必须是源码里的数组（header）；权重体积大、且只需原样搬进内存，用字节流 + SD 加载（mem）可以不重编 ELF 就换网络，更灵活。反过来会让 ELF 膨胀几十 MB，且换网络要全量重编。

**练习 2**：`eepinput.mem` 里的数据是「原始 BMP 像素」吗？

> **答案**：不是。eepBinCvt 的 `--input` 给的是 BMP，但产出 `eepinput.mem` 已经按网络要求做了 mean/norm、定点（exp）、通道重排与 32 字节步长打包，是**硬件可直接消费的输入格式**（与 u4-l4 讲的硬件输入格式一致）。

---

### 4.3 产物与裸机工程的对应：eepnet.h / eepnet.mem / eepinput.mem 如何被消费

#### 4.3.1 概念说明

有了三件套，还要看裸机工程到底怎么用它们。关键是分清两条互斥的数据来源，由 `config.h` 的 `SD_CARD_IS_READY` 和 `FG_INPUT_DATA_SEPERATED` 两个宏切换：

- **`eepnet.h`（元数据）**：永远走「编译期 include」，不分 SD 与否——因为元数据小、且代码必须看见它。
- **`eepnet.mem`（权重）**：若 `SD_CARD_IS_READY`，运行期从 SD `file_read`；否则……（当前工程默认开了 SD，见 [config.h:64](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L64)）。
- **`eepinput.mem`（输入图）**：若 `SD_CARD_IS_READY`，运行期从 SD `file_read`；否则用编译期 include 的 `eepinput[]` 数组（由 `eepinput.h` 提供，`FG_INPUT_DATA_SEPERATED` 控制）。

`FG_INPUT_DATA_SEPERATED` 的字面含义见 [config.h:37-38](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L37-L38)：`1` 表示「网络数据和输入数据分开放（eepnet.h + eepinput.h 两份）」，`0` 表示「全打包进 eepnet.h」。当前值是 1。

#### 4.3.2 核心流程

`main.cc` 启动后消费三件套的顺序：

```
main()
  ├─ #include "net_data/eepnet.h"            （编译期：拿到 eepnet_config[]）
  ├─ eeptpu_init(... eepnet_config, sizeof(eepnet_config))   （解析元数据 → 得到 hwbase0..3/输入输出地址/mean/norm）
  ├─ #ifdef SD_CARD_IS_READY
  │     file_read("eepnet.mem", eepnet, NET_SIZE)           （运行期：权重搬进 hwbase0 段）
  │  #else
  │     eepif.mem_write(...)                                （运行期：从编译期数组搬权重）
  ├─ case '4': 读输入
  │     #ifdef SD_CARD_IS_READY  → file_read("eepinput.mem", eepinput_addr, INPUTDATA_SIZE)
  │     #else                     → eepsa.eeptpu_input(eepinput, sizeof(eepinput))   （从 eepinput.h 数组）
```

#### 4.3.3 源码精读

**① include 元数据**：[main.cc:32](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L32) `#include "net_data/eepnet.h"`，把 `eepnet_config[]` 引入。注意 [main.cc:34-36](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L34-L36) 里 `eepinput.h` 是**被注释掉的**——因为当前走 SD 路线，输入来自 `eepinput.mem`/摄像头，不需要编译期数组。

**② 解析元数据**：[main.cc:288-289](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L288-L289) 调用 `eepsa.eeptpu_init((unsigned char *)0x10000000, 0, eepnet_config, sizeof(eepnet_config))`。第三个参数就是 header 产物，第四个是它的字节数。`eeptpu_init` 内部按 [eeptpu_sa.cpp:106-172](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L106-L172) 的格式逐字段还原出 `hwbase0..3`、输出 shape 列表、输入 shape、mean/norm（细节留给 u3-l3）。解析完后，[main.cc:298-304](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L298-L304) 把 `hwbase0` 等赋给 `eepnet` 指针，作为稍后装权重的目的地址。

**③ 装权重（SD 路线）**：[main.cc:311-317](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L311-L317) 在 `SD_CARD_IS_READY` 下，`file_read("eepnet.mem", eepnet, NET_SIZE)` 把权重从 SD 读到 `eepnet`（即 `hwbase0`）指向的 DDR 地址，随后 `Xil_DCacheFlush`——因为裸机下 CPU 缓存与 DMA/TPU 看到的内存不一致，写完必须冲刷缓存，TPU 才能读到最新权重。

**④ 装输入（二选一）**：[main.cc:514-527](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L514-L527) 的 `case '4'`：SD 就绪时 `file_read("eepinput.mem", eepinput_addr, INPUTDATA_SIZE)`（[main.cc:517](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L517)）；否则 `eepsa.eeptpu_input(eepinput, sizeof(eepinput))`（[main.cc:520](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L520)）从编译期数组搬。`eeptpu_input` 内部见 [eeptpu_sa.cpp:218-230](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L218-L230)，`FG_INPUT_DATA_SEPERATED==1` 时把输入搬到 `hwbase1`。

> 一句话总结消费关系：**eepnet.h → 编译期元数据；eepnet.mem → 运行期权重；eepinput.mem → 运行期测试输入**。三者各司其职，缺一不可。

#### 4.3.4 代码实践

**实践目标**：通过阅读 `main.cc` 和 `config.h`，画出三件套在裸机里的「落点表」，理解每个文件被谁、在哪一步、用什么方式消费。

**操作步骤**：

1. 打开 [config.h:37-47](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L37-L47)，记录 `FG_INPUT_DATA_SEPERATED`、`SD_CARD_IS_READY`、`NET_SIZE`、`INPUTDATA_SIZE` 四个值。
2. 在 `main.cc` 里分别定位 `#include "net_data/eepnet.h"`、`eeptpu_init(... eepnet_config ...)`、`file_read("eepnet.mem"...)`、`file_read("eepinput.mem"...)` 四处。
3. 填出下表（答案见「预期结果」）：

| 产物 | 消费代码行 | 消费方式 | 落点地址/变量 |
| --- | --- | --- | --- |
| eepnet.h | main.cc:32 / 289 | 编译期 include + eeptpu_init 解析 | 还原为 hwbase0..3、addr_out、addr_in 等 |
| eepnet.mem | main.cc:314 | 运行期 file_read | eepnet = (u8*)hwbase0 |
| eepinput.mem | main.cc:517 | 运行期 file_read | eepinput_addr = (u8*)hwbase1 |

**需要观察的现象**：每次 `file_read` 之后都紧跟着一次 `Xil_DCacheFlush`（如 [main.cc:315](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L315)、[main.cc:518](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L518)）。

**预期结果**：能解释「为什么 file_read 后要 flush」——裸机 CPU 的 D-Cache 与 TPU/SD DMA 直访 DDR 不一致，不冲刷的话 TPU 可能读到旧数据。这是裸机部署区别于 Linux 的典型坑。本步为源码阅读型实践，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `config.h` 的 `SD_CARD_IS_READY` 注释掉（走 `#else`），权重从哪里来？

> **答案**：从编译期数组来。`eeptpu_sa.cpp` 的 [eeptpu_sa.cpp:207-212](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L207-L212) 在 `#ifndef SD_CARD_IS_READY` 下用 `eepif.mem_write(wraddr, ...)` 把传入的 `data`（来自编译期 include 的数组）搬进内存。代价是 ELF 会变得很大（权重全编进去）。

**练习 2**：`eepinput.mem` 和摄像头采集的输入，二者关系是什么？

> **答案**：互为替代。`eepinput.mem` 是「固定样例图」的预处理字节流，用于无摄像头时测试；带摄像头时（`case '5'`），输入来自 `dvp_capture` 实时采集并就地预处理，不读 `eepinput.mem`。

---

### 4.4 编译→转换的衔接：bin_name 契约与完整链路

#### 4.4.1 概念说明

最后一块拼图：编译步骤（u3-l1 的 `b_yolo4tiny.sh`）和转换步骤（本讲的 `eepbin_cvt.sh`）是怎么拴在一起的？靠一个**字符串契约**——`setting.ini` 里的 `bin_name`。

`bin_name` 决定了：

- 编译器把产物叫什么名字（`eeptpu_s2.pub.bin`）；
- `b_yolo4tiny.sh` 把它归档到 `binRoot/yolov4tiny/<bin_name>`；
- `eepbin_cvt.sh` 用 `--bin .../<bin_name>` 把它读进来转换。

所以 **`bin_name` 是贯穿「编译 → 归档 → 转换」三步的钥匙**。你在 `setting.ini` 改了 `bin_name`（比如启用 INT8 方案，变成 `nntpu_int8.pub.bin`），就必须同步改 `eepbin_cvt.sh` 里的 `--bin` 路径，否则转换步骤找不到文件。

#### 4.4.2 核心流程

完整链路（含每步工具与产物）：

```
[输入] yolov4_tiny.cfg / yolov4_tiny.weights / 004545.bmp
   │
   │ ① b_yolo4tiny.sh 读 setting.ini，拼命令并 eval eeptpu_compiler
   ▼
eeptpu_s2.pub.bin   （bin_name 来自 setting.ini，--public_bin 标志）
   │ ② b_yolo4tiny.sh: mv 归档到 binRoot/yolov4tiny/
   ▼
binRoot/yolov4tiny/eeptpu_s2.pub.bin
   │ ③ eepbin_cvt.sh: eepBinCvt --output header  /  --output mem
   ▼
eepnet.h  +  eepnet.mem  +  eepinput.mem
   │ ④ 手工拷贝：eepnet.h → src/net_data/；*.mem → SD 卡
   ▼
裸机 main.cc: include eepnet.h + file_read(*.mem) → eeptpu_init → forward
```

#### 4.4.3 源码精读

**契约来源**——`setting.ini`：

[setting.ini:9-10](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/setting.ini#L9-L10) 当前启用的 `s2+sim` 方案定义 `global_cmd=--public_bin --hybp --base_par 0x30000000 ...` 和 `bin_name=eeptpu_s2.pub.bin`。注释掉的其他方案（s2quant/s2t4/s2t2）会给出不同的 `bin_name`（如 `nntpu_int8.pub.bin`、`nntpu_s2.pub.bin`）——这就是为什么换方案要同步改下游脚本。

**编译并归档**——`b_yolo4tiny.sh`：

- [b_yolo4tiny.sh:33-45](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L33-L45) 用迷你 awk 解析器从 `setting.ini` 读出 `compiler/model_root/global_cmd/bin_name` 四个键。
- [b_yolo4tiny.sh:90-100](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L90-L100) 拼出编译命令：`cfg=../models/yolov4tiny/yolov4_tiny.cfg`、`wts=...yolov4_tiny.weights`、`img_ssd=../models/images/004545.bmp`，加上从 ini 来的 `global_cmd`、`--mean`、`--norm`、`--extinfo`（类别表）、`--input_folder`，`eval` 执行。产物名就是 `bin_name`。
- [b_yolo4tiny.sh:63-74](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L63-L74) 先建好 `binRoot/yolov4tiny/` 目录（注意 `b_yolo4tiny.sh` 开头 `cd` 到了 `scripts/`，所以 `binRoot` 建在 `scripts/binRoot`）。
- [b_yolo4tiny.sh:109-112](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L109-L112) `mv ./${bin_name} ${binDir}/${netName}`，把编译出的 `eeptpu_s2.pub.bin` 搬进 `binRoot/yolov4tiny/`。

**转换**——`eepbin_cvt.sh` 的 `--bin ./scripts/binRoot/yolov4tiny/eeptpu_s2.pub.bin`（[eepbin_cvt.sh:3](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/eepbin_cvt.sh#L3)）正是承接上一步归档的那个文件。文件名 `eeptpu_s2.pub.bin` 与 [setting.ini:10](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/setting.ini#L10) 的 `bin_name` 逐字相同——这就是契约的落点。

#### 4.4.4 代码实践

**实践目标**：把整条链路写成可执行的「分步命令清单」，并指出每步用到的工具与产物。

**操作步骤**（以下为「示例命令」，路径以 `sdk/standalone/net_model/` 为基准，实际请按本机调整；运行需要本机有 `eeptpu_compiler` 与 `eepBinCvt` 的执行环境）：

```bash
# —— 第 0 步：确认 setting.ini 当前启用的方案与 bin_name ——
#   打开 scripts/setting.ini，当前为 s2+sim：
#     global_cmd=--public_bin --hybp --base_par 0x30000000 --base_in 0x30000000 --base_out 0x30000000 --base_tmp 0x80000000
#     bin_name=eeptpu_s2.pub.bin

# —— 第 1 步：编译（u3-l1 已讲，这里给 eval 后的等价命令，CWD=scripts/）——
compiler/eeptpu_compiler \
    --public_bin --hybp \
    --base_par 0x30000000 --base_in 0x30000000 --base_out 0x30000000 --base_tmp 0x80000000 \
    --output ./ \
    --mean '0.0,0.0,0.0' --norm '0.003921569,0.003921569,0.003921569' \
    --darknet_cfg models/yolov4tiny/yolov4_tiny.cfg \
    --darknet_weight models/yolov4tiny/yolov4_tiny.weights \
    --image models/images/004545.bmp \
    --extinfo 'classes=background,person,...,toothbrush' \
    --input_folder models/images/ssd/
#   产物：当前目录下生成 eeptpu_s2.pub.bin（名字=bin_name）
mv ./eeptpu_s2.pub.bin binRoot/yolov4tiny/

# —— 第 2 步：转换（本讲，CWD=net_model/，自洽写法）——
eepBinCvt/eepBinCvt \
    --bin scripts/binRoot/yolov4tiny/eeptpu_s2.pub.bin \
    --input models/images/ssd/004545.bmp \
    --output header          # 产出 eepnet.h
eepBinCvt/eepBinCvt \
    --bin scripts/binRoot/yolov4tiny/eeptpu_s2.pub.bin \
    --input models/images/ssd/004545.bmp \
    --output mem             # 产出 eepnet.mem + eepinput.mem

# —— 第 3 步：把产物搬进裸机工程 ——
cp eepnet.h      ../src/net_data/eepnet.h        # 供 main.cc #include
cp eepnet.mem    <SD 卡>/eepnet.mem              # 供 file_read
cp eepinput.mem  <SD 卡>/eepinput.mem            # 供 file_read
```

**需要观察的现象**：

- 第 1 步若成功，`scripts/binRoot/yolov4tiny/` 下应出现 `eeptpu_s2.pub.bin`。
- 第 2 步 `--output header` 应产出顶部带 `Generated by eepBinCvt(...)` 注释的 `eepnet.h`；`--output mem` 应产出数兆级的两个 `.mem`。
- 第 3 步后，`sdk/standalone/src/net_data/eepnet.h` 与 SD 卡上的两个 `.mem` 齐备，`main.cc` 即可 `#include` 与 `file_read`。

**预期结果**：链路打通后，裸机工程在板上 `eeptpu_init` 成功（说明 `eepnet_config` 解析通过），`file_read` 后 `forward` 能出结果。若本机无编译器/转换器运行环境，整条链路**待本地验证**；但仓库已提交一份成品三件套（`sdk/standalone/src/net_data/`），可跳过第 1、2 步直接进 Vitis 验证第 3 步以后的裸机行为。

#### 4.4.5 小练习与答案

**练习 1**：把 `setting.ini` 切到 `s2quant+sim`（INT8）方案后，必须同步改哪些地方？

> **答案**：至少改两处。① `eepbin_cvt.sh` 里 `--bin` 的文件名要从 `eeptpu_s2.pub.bin` 改成 `nntpu_int8.pub.bin`（新方案的 `bin_name`，见 [setting.ini:14](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/setting.ini#L14)）。② 重新生成的 `eepnet.mem` 体积会变（INT8 权重更小），需同步更新 `config.h` 的 `NET_SIZE`（[config.h:46](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L46)），否则 `file_read` 读数不对。

**练习 2**：为什么 `eepbin_cvt.sh` 要把 `--input` 指向一张具体的 BMP（`004545.bmp`），而不是像编译器那样用 `--input_folder`？

> **答案**：因为 `eepinput.mem` 只装「一张固定测试图」的硬件输入字节流，供无摄像头时跑单张验证，所以只需要一张图（`--input`）。而编译器的 `--input_folder` 是给量化/仿真校准用的一组图，两者目的不同。

---

## 5. 综合实践

**任务**：绘制并写出「从 `yolov4_tiny.cfg/.weights` 到裸机 `main.cc` 里一次成功 `forward`」的完整数据流图与命令清单，并在关键节点标注「谁产出、谁消费、什么格式」。

**要求**：

1. 画出四个阶段的方框图：① 编译（eeptpu_compiler）② 归档（b_yolo4tiny.sh 的 mv）③ 转换（eepBinCvt 的 header/mem）④ 裸机消费（include + file_read + eeptpu_init + forward）。
2. 在每个箭头上标出**产物文件名**与**格式特征**（如 `pub.bin`=一体化硬件二进制；`eepnet.h`=C 元数据数组；`eepnet.mem`=权重字节流）。
3. 用一句话回答：如果我想不重编 ELF、只换一张测试图来跑，应该替换哪个产物、放哪里？（提示：`eepinput.mem` → SD 卡）
4. 用一句话回答：如果我想换网络（比如换成 mobilenet-ssd），需要重新跑哪几个步骤、改哪几个文件名契约？（提示：第①②③步都要重跑；`bin_name`、`eepbin_cvt.sh` 的 `--bin`、`config.h` 的 `NET_SIZE/INPUTDATA_SIZE/NET_TYPE` 都要同步）

**参考答案要点**：

- 第 3 题：替换 SD 卡上的 `eepinput.mem` 即可，它是唯一的运行期测试输入；`eepnet.h`（元数据）和 `eepnet.mem`（权重）都不用动。
- 第 4 题：重跑①②③（重新编译→归档→转换），并把 `setting.ini` 的 `bin_name`、`eepbin_cvt.sh` 的 `--bin` 路径、`config.h` 的 `NET_TYPE/NET_SIZE/INPUTDATA_SIZE` 全部同步成新网络的值；最后把新 `eepnet.h` 拷进 `src/net_data/`、新 `.mem` 拷进 SD 卡。

## 6. 本讲小结

- **eepBinCvt 是主机侧（x86-64）格式转换器**，把一体化的 `*.pub.bin` 拆成裸机工程能用的三件套；它不是再编译，而是解包/重排。
- **裸机需要这次转换**，是因为它没有 Linux 运行库 `libeeptpu_pub` 的运行时解析能力，只能走「编译期 include 数组」+「运行期 file_read 字节流」两条路。
- **两种输出模式**：`--output header` 产出 `eepnet.h`（`eepnet_config[]` **元数据**数组，体积小），`--output mem` 产出 `eepnet.mem`（权重）+ `eepinput.mem`（测试图硬件输入），体积大。
- **消费关系**：`eepnet.h` 被 `main.cc` include 并交 `eeptpu_init` 解析；`eepnet.mem`/`eepinput.mem` 在 `SD_CARD_IS_READY` 下被 `file_read` 读到 `hwbase0`/`hwbase1`，读后必须 `Xil_DCacheFlush`。
- **`bin_name` 是贯穿编译→归档→转换的字符串契约**，换编译方案（如 INT8）必须同步改 `eepbin_cvt.sh` 的 `--bin` 与 `config.h` 的尺寸常量。
- 仓库已提交一份成品三件套（`sdk/standalone/src/net_data/`），即使本机无编译器/转换器，也能直接进 Vitis 验证裸机侧。

## 7. 下一步学习建议

本讲把 `*.pub.bin` 翻译成了 `eepnet.h` 的 `eepnet_config[]`，但这个数组里的字节到底怎么被还原成输入输出 shape、硬件地址、mean/norm，我们只是点到为止。下一讲 **u3-l3《eepnet 配置数组格式解析》** 会逐字段拆开 `eepnet_config[]` 与 `eeptpu_sa.cpp` 的 `eeptpu_init`，把元数据表的二进制布局讲透——它是衔接「编译产物」与「裸机寄存器驱动」的最后一块拼图。

之后进入 U4 单元，正式开始读裸机驱动：建议按 u4-l1（standalone 工程结构）→ u4-l2（EEPTPU_SA 类与寄存器协议）→ u5-l1（forward 时序）的顺序，把本讲产出的权重和输入「真正喂进 TPU 跑起来」。如果想从 Linux 侧对照理解，可回顾 u2-l3 的 `EEPTPU` 高层 API——你会发现 `load_bin` 在那里做的事，正是 eepBinCvt + `eeptpu_init` + `file_read` 在裸机侧手工拆开做的事。
