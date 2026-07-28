# 平台适配：Windows / wasm / Android

## 1. 本讲目标

本讲聚焦「同一份 C++ 源码，如何在不同操作系统与运行环境上正确编译与运行」。学完后你应该能够：

- 读懂 `xmake.lua` 里按平台切换的构建分支，说出 `NOMINMAX`、`/utf-8`、`emscripten` 链接参数分别解决什么问题。
- 理解为什么 Windows 上要把命令行参数单独「翻译」成 UTF-8，并能复述 `get_utf8_args` / `enable_utf8_console` 的工作原理。
- 掌握 wasm（emscripten）目标的工具链切换、内存上限与运行时方法导出配置。
- 认清本仓库「哪些平台是 ncnn_llm 自己做特化、哪些是依赖 ncnn 兜底」，避免被 README 平台徽章误导。

本讲对应「专家层」的二次开发能力——当你想把项目移植到一个新平台，或在某个平台上遇到中文乱码、内存溢出、链接报错时，这里的知识就是排查起点。

## 2. 前置知识

本讲假设你已经读过 [u1-l2 构建系统与运行方式](u1-l2-build-and-run.md)，了解 xmake 的 `target`、`add_requires`、`add_packages` 等基本概念。下面补充几个跨平台特有的基础概念。

### 2.1 字符编码与「代码页」

计算机底层只存数字，字符编码规定了「数字 ↔ 字符」的映射表。

- **ASCII**：只用 1 字节、128 个字符，覆盖英文字母与控制符，不含中文。
- **UTF-8**：变长编码（1～4 字节），是互联网与 Linux/macOS 的事实标准，能表示全世界文字。中文「你」在 UTF-8 下是 3 个字节 `0xE4 0xBD 0xA0`。
- **GBK / ANSI 代码页**：Windows 中文环境的「传统」编码，中文用 2 字节。同一个字节序列，按 UTF-8 解读和按 GBK 解读会得到完全不同的字符，这就是乱码的根源。
- **宽字符（wide char / `wchar_t`）**：Windows 上是 UTF-16（每字符 2 字节），Linux 上通常是 4 字节。Windows API 提供一批以 `W` 结尾的宽字符版本（如 `GetCommandLineW`）来规避编码歧义。

关键痛点：当 Windows 上的 `.exe` 被启动时，标准 C 的 `argc/argv` 拿到的是**按 ANSI 代码页解释**的参数。如果你的命令行里带了中文（例如模型路径 `assets/通义千问`），经过 ANSI 解释后再当作 UTF-8 字符串使用，就会变成乱码，导致文件找不到。本讲的 `utf8_args.h` 就是为了修复它。

### 2.2 编译选项：源码字符集 vs 执行字符集

C++ 源码里的字符串字面量（如 `"你好"`）也涉及两层编码：

- **源码字符集（source charset）**：`.cpp` 文件本身存成什么编码。
- **执行字符集（execution charset）**：编译后写进可执行文件里的字节序列。

MSVC（Windows 上的微软编译器）默认这两层都不是 UTF-8，会把中文源码里的字面量搞乱。`/utf-8` 选项一次性把这两层都设成 UTF-8。而 GCC/Clang 默认就是 UTF-8，不需要这个标志。

### 2.3 min/max 宏

Windows 头文件 `<windows.h>` 里历史上用宏定义了 `min` 与 `max`：

```cpp
#define min(a,b) (((a) < (b)) ? (a) : (b))
#define max(a,b) (((a) > (b)) ? (a) : (b))
```

这会与 C++ 标准库 `std::min` / `std::max`，以及 `std::numeric_limits<int>::max()` 这类写法冲突——`max(...)` 会被宏展开成乱码。定义 `NOMINMAX` 宏可以在包含 `<windows.h>` 时禁止它定义这两个宏。

### 2.4 WebAssembly 与 emscripten

**WebAssembly（wasm）** 是一种运行在浏览器（或 Node.js 等）里的低级虚拟指令集。把 C++ 编译成 wasm，就能在网页里直接跑 ncnn_llm。**emscripten** 是把 C/C++ 编译成 wasm 的工具链，它提供一个「虚拟文件系统（FS）」、内存管理、以及对 JavaScript 互操作的运行时。wasm 与普通桌面程序最大的不同是：**它默认内存上限较低，且不会自动暴露运行时函数给 JS 调用**，这两点都需要在链接期显式配置。

### 2.5 Android 的真相（先打个预防针）

README 顶部的徽章写着 `platform: Windows | Linux | Android`，但这**不代表 ncnn_llm 自己写了 Android 特化代码**。事实是：`xmake.lua` 里**没有 `is_plat("android")` 分支**。Android 之所以能跑，是因为底层依赖 ncnn，而 ncnn 本身支持 Android（NDK + arm/armv8 工具链）。换句话说，ncnn_llm 真正动手做平台特化的只有 **Windows（UTF-8）** 和 **wasm（内存/导出）** 两处。这是一个非常重要的辨别点，下文会反复回到它。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `xmake.lua` | 构建配置总入口，所有平台分支都集中在这里。 |
| `examples/utf8_args.h` | Windows GBK 环境下的命令行 UTF-8 救援（`get_utf8_args` / `enable_utf8_console`）。 |
| `examples/llm_ncnn_run/main.cpp` | 示例入口，演示如何调用 utf8_args 两个函数。 |
| `examples/ocr_main.cpp` / `examples/asr_main.cpp` | OCR / ASR 示例，复用同一套 UTF-8 处理。 |
| `readme.md` / `README_CN.md` | 平台徽章所在，仅做声明，不构成构建依据。 |

> 说明：规格里提到的 `examples/llm_ncnn_run/utf8_args.h` 与 `examples/llm_ncnn_run/util.cpp` 在实际仓库中并不在该子目录下。UTF-8 处理的真正落点是 `examples/utf8_args.h`（位于 `examples/` 根目录），而 `examples/llm_ncm_run/util.cpp` 只含 `parse_int` / `now_ms_epoch` 等小工具、与平台编码无关。本讲以**真实文件**为准。

## 4. 核心概念与源码讲解

### 4.1 `set_encodings`：为什么连源码文件编码也要管

#### 4.1.1 概念说明

`set_encodings("utf-8")` 是 xmake 的内置函数，作用是**告诉构建系统：本项目所有源码文件（`.cpp/.h/.lua` 等）都以 UTF-8 编码保存**。这看似理所当然，但在 Windows 工具链上却有实际意义：

- 它让 xmake 在调用编译器时，对**源码字符集**给出正确提示，避免 MSVC 误判文件编码。
- 它同时影响 xmake 自身读取 `xmake.lua` 等配置文件时的解码方式，保证 `xmake.lua` 里若出现中文注释也能正确解析。

注意它管的是「文件存盘编码」，与下文 4.2 的 `/utf-8`（管编译产物里的字符串字面量）是两个不同层面。

#### 4.1.2 核心流程

```text
set_encodings("utf-8")   # 全局声明，写在 xmake.lua 顶部
        │
        ▼
xmake 读取源码文件时按 UTF-8 解码
        │
        ▼
传给编译器时附带正确的源码字符集信息
```

#### 4.1.3 源码精读

这是 `xmake.lua` 的第 4 行，位于所有 target 定义之前、属于全局设置：

[xmake.lua:4-4](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L4-L4) —— 全局声明源码文件编码为 UTF-8。

紧接着的两行设定语言标准，同样属于全局基础设置：

[xmake.lua:6-6](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L6-L6) —— 要求 C++20 / C11 标准，这是平台无关的硬性要求。

#### 4.1.4 代码实践

1. **实践目标**：确认 `set_encodings` 的作用域是全局、且先于 target 生效。
2. **操作步骤**：打开 `xmake.lua`，确认 `set_encodings("utf-8")` 写在第 1～6 行的全局区，没有被包进任何 `target(...)` 块里。
3. **观察现象**：尝试把一行中文注释加进某个 `.cpp` 文件，分别在 Windows（MSVC）与 Linux（GCC）下 `xmake build`。
4. **预期结果**：两端都能正常编译、且运行输出中文不乱码——这背后就有 `set_encodings` 与下文 `/utf-8` 的共同作用。
5. 若手头无 Windows 环境，可只做「源码阅读」确认其全局位置，**待本地验证**编译行为。

#### 4.1.5 小练习与答案

- **练习 1**：`set_encodings("utf-8")` 与 MSVC 的 `/utf-8` 选项是同一回事吗？
  - **答案**：不是。`set_encodings` 是 xmake 层面对「源码文件存盘编码」的声明（并影响 xmake 读配置）；`/utf-8` 是编译器层面把「源码字符集」和「执行字符集」都设为 UTF-8。二者互补。

- **练习 2**：为什么 `set_encodings` 要写在所有 `target(...)` 之前？
  - **答案**：它是全局设置，影响整个工程的所有 target；写在全局区才能对所有目标统一生效。

### 4.2 Windows 分支：`NOMINMAX` 宏与 `/utf-8` 编译选项

#### 4.2.1 概念说明

Windows 平台（无论用 MSVC 还是 MinGW）需要处理两个 C++ 与 Windows SDK 的历史遗留冲突，本仓库用 `is_plat("windows")` / `is_plat("windows", "mingw")` 两个分支解决：

1. **`min`/`max` 宏污染**：用 `NOMINMAX` 宏禁用。
2. **中文源码字面量乱码**：用 `/utf-8` 编译选项。
3. **缺少系统库链接**：图像/窗口相关代码需要 `user32`、`gdi32`、`shell32` 三个 Windows 系统库。

#### 4.2.2 核心流程

```text
is_plat("windows") ──► add_defines("NOMINMAX")          # 禁用 min/max 宏
                  └─► add_cxflags("/utf-8")              # 源码+执行字符集设为 UTF-8
is_plat("windows","mingw") ─► add_syslinks("user32","gdi32","shell32")  # 补系统库
```

> **现状提示**：`xmake.lua` 里这段配置出现了**重复**（见 4.2.3）。xmake 的 `add_*` 系列函数是幂等的「追加」语义，重复写两次只是把同一选项追加两次，实际效果等同于一次，不会报错——但确实属于可以清理的多余代码。读源码时若看到一模一样的块，不要怀疑自己看错。

#### 4.2.3 源码精读

第一个 Windows 块，处理 `NOMINMAX` 与 `/utf-8`：

[xmake.lua:32-37](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L32-L37) —— 对 `windows` 平台定义 `NOMINMAX` 宏，并给 C/C++ 编译器加 `/utf-8`。

第二个块覆盖 `windows` 与 `mingw`，补系统库并再次加 `/utf-8`（与上一块部分重叠）：

[xmake.lua:39-44](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L39-L44) —— 链接 `user32`（窗口/消息）、`gdi32`（图形绘制）两个系统库，并对 mingw 也补 `/utf-8`。

第三个块（与第二个的 syslinks 部分重复）：

[xmake.lua:46-48](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L46-L48) —— 再次为 `windows`/`mingw` 链接 `user32`、`gdi32`，属幂等重复。

此外，三个会读图/调系统 API 的 target（`llm_ncnn_run`、`ocr_main`、`asr_main`）各自单独补了 `shell32`（用于 `SHGetFolderPath` 等外壳 API）。以 `llm_ncnn_run` 为例：

[xmake.lua:81-83](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L81-L83) —— 仅在 `windows`/`mingw` 下为 `llm_ncnn_run` 追加 `shell32` 系统库。

`/utf-8` 之所以同时用 `add_cxflags`（C）和 `add_cxxflags`（C++）各写一遍，是因为本仓库同时存在 `.c` 与 `.cpp`（例如 `src/utils/stb_image.h` 配套的 C 代码），需要分别覆盖两种语言。

#### 4.2.4 代码实践

1. **实践目标**：亲身感受 `NOMINMAX` 与 `/utf-8` 各自的必要性。
2. **操作步骤**：
   - 写一个最小 `.cpp`，`#include <windows.h>` 后调用 `std::numeric_limits<int>::max()`；在 `xmake.lua` 临时注释掉 `add_defines("NOMINMAX")`，用 MSVC 编译。
   - 再写一个含中文字面量 `std::string s = "你好";` 的小程序，注释掉 `/utf-8` 后编译并打印 `s.size()`。
3. **观察现象**：前者会报宏展开相关的编译错误；后者 `s.size()` 可能不是 6（UTF-8 下「你好」应为 6 字节）。
4. **预期结果**：恢复 `NOMINMAX` 与 `/utf-8` 后，两个问题都消失。**待本地 Windows 环境验证**。
5. 若无 Windows 环境，做「源码阅读型实践」：在 `xmake.lua` 里数出 `/utf-8` 出现了几次、分别由哪几个 `is_plat` 条件触发。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `NOMINMAX` 要用 `add_defines`（预处理器宏），而 `/utf-8` 要用 `add_cxflags`（编译选项）？
  - **答案**：`NOMINMAX` 是给预处理器看的——它在 `<windows.h>` 内部被 `#ifdef` 检查，决定是否定义 `min`/`max` 宏，所以必须走 `-D` 宏定义；`/utf-8` 是编译器前端控制源码/执行字符集的开关，属于编译选项，故走 `cxflags`。

- **练习 2**：`user32`、`gdi32`、`shell32` 三个系统库里，哪个是「所有 Windows 程序都需要」、哪个是「只有读图/调外壳 API 的 target 才需要」？
  - **答案**：`user32`/`gdi32` 在全局 windows/mingw 分支加给所有 target（ncnn 或窗口基础功能会用到）；`shell32` 只在 `llm_ncnn_run`/`ocr_main`/`asr_main` 三个读文件、调外壳 API 的 target 里单独追加。

### 4.3 UTF-8 参数处理：`get_utf8_args` 与 `enable_utf8_console`

#### 4.3.1 概念说明

4.2 解决了**编译期**的字符集问题，但还有**运行期**的一个坑：Windows 上 `main(int argc, char** argv)` 收到的 `argv` 是按 **ANSI 代码页**解释的。在中文 Windows（GBK 代码页）下，命令行里输入的中文（模型路径、prompt）会被「翻译」成 GBK 字节序列塞进 `argv`；而程序内部把字符串当 UTF-8 用，于是乱码、文件找不到。

`utf8_args.h` 提供两个内联函数（header-only，无 .cpp）来根治它：

- **`get_utf8_args`**：绕过 ANSI 的 `argv`，直接从 Windows 宽字符命令行重新取出参数并转成 UTF-8。
- **`enable_utf8_console`**：把控制台的输入/输出代码页切到 UTF-8，保证 `std::cout`/`std::cin` 的中文不乱码。

两者都用 `#ifdef _WIN32` 守护，在非 Windows 平台是空操作——因此同一份 `main.cpp` 可以跨平台编译。

#### 4.3.2 核心流程

```text
Windows 启动 exe，argv 按 ANSI(GBK) 解释 ──► 中文参数乱码
        │
        │ get_utf8_args(argc, argv) 介入：
        ▼
GetCommandLineW()              # 取原始 UTF-16 宽字符整条命令行
        │
        ▼
CommandLineToArgvW(...)         # 按宽字符规则切分成 argv 数组（正确）
        │
        ▼
WideCharToMultiByte(CP_UTF8,…)  # 每个宽字符参数转成 UTF-8 字节
        │
        ▼
返回 std::vector<std::string>（真正的 UTF-8 参数）
```

非 Windows 分支则直接 `for (i=0..argc) args.push_back(argv[i])` 原样拷贝，因为 Linux/macOS 的 `argv` 本就是 UTF-8。

#### 4.3.3 源码精读

`get_utf8_args` 的 Windows 分支是核心。它先用 `CommandLineToArgvW(GetCommandLineW(), &wargc)` 拿到宽字符参数数组：

[examples/utf8_args.h:21-24](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/utf8_args.h#L21-L24) —— 用宽字符版 API 取回原始命令行并切分，绕开 ANSI `argv`。

再逐个用 `WideCharToMultiByte(CP_UTF8, ...)` 把宽字符转 UTF-8。注意它先传 `nullptr` 调一次「量长度」，再分配 `string` 调第二次真正转换，这是 Windows 宽窄转换的标准两步法：

[examples/utf8_args.h:26-32](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/utf8_args.h#L26-L32) —— 先探测目标字节数 `n`，分配 `n-1` 长度（去掉末尾 `\0`）的字符串，再写入 UTF-8 字节。

非 Windows 的回退分支——原样拷贝：

[examples/utf8_args.h:37-38](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/utf8_args.h#L37-L38) —— Linux/macOS 下 `argv` 已是 UTF-8，直接拷贝。

`enable_utf8_console` 把控制台代码页切到 UTF-8：

[examples/utf8_args.h:43-48](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/utf8_args.h#L43-L48) —— `SetConsoleOutputCP(CP_UTF8)` 管输出（`cout`）、`SetConsoleCP(CP_UTF8)` 管输入（`cin`）；非 Windows 为空。

`main.cpp` 把两者串进启动流程，再把 `vector<string>` 转回 `char*` 数组喂给沿用 C 风格签名的 `parse_options`：

[examples/llm_ncnn_run/main.cpp:27-33](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp#L27-L33) —— 先切控制台 UTF-8，再用 `get_utf8_args` 取真 UTF-8 参数，转成 `char**` 后交给 `parse_options`。

OCR / ASR 示例复用同一套模式：

[examples/ocr_main.cpp:8-9](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/ocr_main.cpp#L8-L9) —— OCR 入口同样先 `enable_utf8_console()` 再 `get_utf8_args()`，保证中文 `--prompt` / `--image` 路径不乱码。

#### 4.3.4 代码实践

1. **实践目标**：直观看到「不经 `get_utf8_args` 会乱码、用了就正常」的差别。
2. **操作步骤**：在中文 Windows（GBK 控制台）下，给 `llm_ncnn_run` 传一个含中文的 `--prompt 你好`，先用现状跑；再在 `main.cpp` 临时把 `get_utf8_args(argc, argv)` 换回直接用原始 `argv`，重新编译再跑。
3. **观察现象**：换回原始 `argv` 后，程序内部拿到的 `--prompt` 值会变成乱码字节（模型分词时多半报错或输出无意义内容）。
4. **预期结果**：恢复 `get_utf8_args` 后中文恢复正常。**待本地 Windows 验证**。
5. 若无 Windows 环境，做源码阅读实践：跟踪 `get_utf8_args` 在 `main.cpp`、`ocr_main.cpp`、`asr_main.cpp` 三个入口的调用位置，确认三者启动顺序一致（都是 main 的第一、二行）。

#### 4.3.5 小练习与答案

- **练习 1**：`WideCharToMultiByte` 为什么要调用两次（第一次传 `nullptr`）？
  - **答案**：第一次只用来「探测目标需要多少字节」（返回长度 `n`），据此分配 `std::string`；第二次才真正把宽字符写进去。这是 Windows 宽→窄转换的标准做法，避免缓冲区溢出或截断。

- **练习 2**：`enable_utf8_console` 在 Git Bash 里可能「没效果」，为什么？
  - **答案**：文件注释已说明——Git Bash 走的是 pty（伪终端）而非传统 Windows 控制台，`SetConsoleOutputCP` 对 pty 无作用；此时 Git Bash 本身已按 UTF-8 处理 I/O，所以「没效果」反而是正常状态。

- **练习 3**：`get_utf8_args` 在 Linux 上为什么直接拷贝 `argv` 即可？
  - **答案**：Linux/macOS 的进程参数本身就是按 UTF-8 字节传递的，没有 ANSI 代码页这层「二次解释」，所以无需转换。

### 4.4 wasm / emscripten 分支：内存上限与运行时方法导出

#### 4.4.1 概念说明

把 ncnn_llm 编译成 wasm 在浏览器里跑，需要 emscripten 工具链。wasm 与桌面程序有两点关键差异，对应 `xmake.lua` 里的两类配置：

1. **运行时方法默认不导出**：emscripten 提供的虚拟文件系统 `FS`、`ccall` 等功能，默认不会暴露给 JavaScript 调用。要用就必须在链接期通过 `-sEXPORTED_RUNTIME_METHODS=[...]` 显式声明。ncnn_llm 加载模型文件依赖 `FS`，所以必须导出它。
2. **内存上限受限**：wasm 默认线性内存很小。wasm32 最多 2GB（实际可用更少），而加载 LLM 权重 + KV cache 动辄上 GB，所以 wasm64 目标要显式抬高初始/最大内存。

另外 `-sASSERTIONS=2` / `-sDEMANGLE_SUPPORT=1` 是面向调试：前者开启运行时断言、后者让 C++ 异常类型名可读，便于排查 wasm 里的崩溃。

#### 4.4.2 核心流程

```text
is_plat("wasm")
   ├── add_requires("emscripten")          # 拉取 emscripten 工具链包
   ├── set_toolchains("emcc@emscripten")    # 切到 emcc 编译器
   └── add_ldflags(                          # 链接期配置运行时
          -sASSERTIONS=2                     # 开启断言
          -sDEMANGLE_SUPPORT=1               # 异常名可读
          -sEXPORTED_RUNTIME_METHODS=['FS']) # 导出虚拟文件系统给 JS

is_plat("wasm") & is_arch("wasm64")         # 64 位 wasm 额外抬内存
   ├── add_cxflags/ldflags(-sMEMORY64=1, -sWASM_BIGINT=1)
   └── add_ldflags(
          -sINITIAL_MEMORY=1073741824        # 初始内存 = 1 GiB
          -sMAXIMUM_MEMORY=17179869184)      # 最大内存 = 16 GiB
```

内存数值用 2 的幂表示更直观：

\[ \text{INITIAL\_MEMORY} = 1\,073\,741\,824 = 2^{30}\ \text{B} = 1\ \text{GiB} \]

\[ \text{MAXIMUM\_MEMORY} = 17\,179\,869\,184 = 2^{34}\ \text{B} = 16\ \text{GiB} \]

> **现状提示**：与 4.2 类似，wasm 的两个分支在 `xmake.lua` 里也各重复了一次（第 8-12 行与第 20-24 行相同，第 14-18 行与第 26-30 行相同）。这是幂等重复，效果等同写一次，属于可清理的多余代码。

#### 4.4.3 源码精读

第一个 wasm 块——工具链与运行时方法导出：

[xmake.lua:8-12](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L8-L12) —— 拉取 emscripten、切到 `emcc` 工具链、并链接期导出 `FS`（虚拟文件系统）等运行时方法。

wasm64 专属块——64 位与内存上限：

[xmake.lua:14-18](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L14-L18) —— 仅 `wasm64` 架构开启 `MEMORY64`/`WASM_BIGINT`，并把初始内存设为 1 GiB、上限设为 16 GiB。

（第 20-30 行是上述两块的逐字重复，见 4.4.2 的现状提示。）

各 emscripten 链接标志的含义对照：

| 标志 | 解决的问题 |
|------|-----------|
| `-sASSERTIONS=2` | 开启 emscripten 运行时断言，wasm 里越界/误用会给出可读报错而非静默崩溃。 |
| `-sDEMANGLE_SUPPORT=1` | C++ 异常的类名做 demangle，崩溃栈里能看到真实类型名。 |
| `-sEXPORTED_RUNTIME_METHODS=['FS']` | 把虚拟文件系统 `FS` 暴露给 JS，否则无法从 JS 侧喂模型文件给 wasm。 |
| `-sMEMORY64=1` / `-sWASM_BIGINT=1` | 启用 64 位 wasm 内存与 BigInt 互操作，突破 wasm32 的 ~2GB 上限。 |
| `-sINITIAL_MEMORY` / `-sMAXIMUM_MEMORY` | 分别设定线性内存的初始值与可增长上限。 |

#### 4.4.4 代码实践

1. **实践目标**：理解去掉某项 wasm 配置会引发什么问题。
2. **操作步骤**（源码阅读型 + 可选运行）：
   - 阅读 `xmake.lua:8-18`，把每个 `-s...` 标志对应到 4.4.3 表格里的「解决的问题」。
   - 可选：若已装 emscripten 与 ncnn 的 wasm 版，执行 `xmake f -p wasm` 配置、`xmake build` 构建，观察产物。
   - 思考实验：若把 `-sEXPORTED_RUNTIME_METHODS=['FS']` 删掉，加载模型时会发生什么？
3. **观察现象**（思考实验预期）：删掉 `FS` 导出后，JS 侧无法把 `.param`/`.bin`/`model.json` 写进 wasm 的虚拟文件系统，模型加载会在找不到文件处失败。
4. **预期结果**：能口述「每项配置各自解决一个 wasm 特有的限制」。
5. 若无 wasm 运行环境，标注**待本地验证**，仅完成源码阅读与对照表填写。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `INITIAL_MEMORY` 与 `MAXIMUM_MEMORY` 只在 `wasm64` 分支设置，wasm32 不设？
  - **答案**：wasm32 的线性内存理论上限约 4 GiB、实际受浏览器限制更小，抬高内存意义有限且可能触发上限；wasm64 才有真正的超大地址空间，值得把初始内存设到 1 GiB、上限 16 GiB 以容纳 LLM 权重。

- **练习 2**：`-sEXPORTED_RUNTIME_METHODS=['FS']` 里的 `FS` 具体指什么？为什么 ncnn_llm 必须导出它？
  - **答案**：`FS` 是 emscripten 提供的虚拟文件系统（MEMFS）的 JS 接口。ncnn_llm 通过文件路径加载 `.param`/`.bin`/`model.json`，在 wasm 里这些「文件」其实存在于 `FS` 中，需要 JS 先把数据写进 `FS`，所以必须把 `FS` 导出给 JS 侧调用。

- **练习 3**：`-sASSERTIONS=2` 通常只用在调试，发布时关掉。为什么本仓库默认开着？
  - **答案**：wasm 的崩溃信息极不直观（往往只是一个 trap），开着断言能大幅降低排查难度；这是开发期便利与发布期性能/体积的取舍，本仓库当前偏向可调试性。

## 5. 综合实践

把本讲的四个模块串起来，完成一份「平台适配清单」。

**任务**：通读 `xmake.lua` 的全部平台分支，并对照 `utf8_args.h` 与 `main.cpp`，整理出下表并回答三个问题。

| 平台 | 触发条件 (`is_plat`) | 关键配置 | 解决的问题 |
|------|----------------------|----------|-----------|
| Windows（MSVC） | `is_plat("windows")` | `NOMINMAX`、`/utf-8` | min/max 宏污染、中文源码字面量 |
| Windows/MinGW | `is_plat("windows","mingw")` | `user32`/`gdi32`/`shell32` | 缺系统库链接 |
| wasm | `is_plat("wasm")` | emcc 工具链、`FS` 导出、断言 | 工具链切换、JS 文件系统互操作 |
| wasm64 | `is_plat("wasm") and is_arch("wasm64")` | `MEMORY64`、1 GiB/16 GiB 内存 | 突破 32 位内存上限 |
| Android | —— | **（无）** | 依赖 ncnn 自身工具链，ncnn_llm 不特化 |

**请回答**：

1. **`NOMINMAX`、`/utf-8`、`emscripten -sINITIAL_MEMORY`、`-sEXPORTED_RUNTIME_METHODS` 各解决什么问题？**
   - 参考：`NOMINMAX` 禁用 Windows 的 `min`/`max` 宏以兼容 `std::min/max`；`/utf-8` 让 MSVC 把源码与执行字符集都按 UTF-8 处理；`-sINITIAL_MEMORY` 抬高 wasm 线性内存初始值以容纳模型权重；`-sEXPORTED_RUNTIME_METHODS=['FS']` 把虚拟文件系统暴露给 JS 以便喂入模型文件。

2. **`utf8_args` 在 Windows GBK 环境下为何需要显式处理命令行参数？**
   - 参考：Windows 的 `main(argc, argv)` 收到的 `argv` 按 ANSI（中文 Windows 为 GBK）代码页解释，中文参数会变成 GBK 字节序列；而程序内部按 UTF-8 处理字符串，直接用会乱码。`get_utf8_args` 绕过 ANSI 通道，从宽字符命令行 `GetCommandLineW` 重新取值并转成 UTF-8，根治该问题。

3. **README 徽章写着支持 Android，但 `xmake.lua` 里找不到 Android 分支，这矛盾吗？**
   - 参考：不矛盾但需辨清。「能跑在 Android 上」来自底层依赖 ncnn 对 Android 的官方支持（NDK 工具链），ncnn_llm 只是一个上层应用，未引入 Android 专属构建逻辑。换言之，ncnn_llm 真正动手做平台特化的只有 Windows 与 wasm 两处，Android 走 ncnn 的通用路径。

完成后，建议把你整理的这张表保存进个人笔记——它是日后移植到任何新平台时的排查索引。

## 6. 本讲小结

- **`set_encodings("utf-8")`** 是 xmake 层面对「源码文件存盘编码」的全局声明，先于所有 target 生效，与编译器的 `/utf-8` 是不同层面。
- **Windows 分支**用 `NOMINMAX` 宏禁用 `min/max` 宏污染、用 `/utf-8` 让 MSVC 正确处理中文字面量，并用 `user32`/`gdi32`/`shell32` 补齐系统库链接。
- **`utf8_args.h`** 是运行期的 UTF-8 救援：`get_utf8_args` 从宽字符命令行重取 UTF-8 参数、修复 GBK 环境下中文 `argv` 乱码；`enable_utf8_console` 切换控制台代码页。两者均用 `#ifdef _WIN32` 守护、跨平台编译。
- **wasm / emscripten 分支**切换 emcc 工具链、用 `-sEXPORTED_RUNTIME_METHODS=['FS']` 导出虚拟文件系统、用断言/demangle 提升可调试性；wasm64 额外把初始内存抬到 1 GiB、上限 16 GiB。
- **`xmake.lua` 存在多处幂等重复块**（windows、wasm、syslinks 都写了两遍），无害但可清理——读源码时不要被重复迷惑。
- **Android 无特化分支**：README 徽章虽列 Android，但实际依赖 ncnn 自身的 Android 工具链支持，ncnn_llm 真正动手的只有 Windows 与 wasm。

## 7. 下一步学习建议

- 若想继续深入「在浏览器里跑推理」的完整链路，建议结合 [u8-l1 Vulkan GPU 推理与线程/精度配置](u8-l1-vulkan-threads.md) 理解 GPU/CPU 执行配置后，再尝试用 emscripten 实际构建一个 wasm target，观察 `FS` 如何从 JS 侧写入模型文件。
- 想验证本讲的 UTF-8 修复，可回到 [u1-l4 CLI 入口、选项解析与 UTF-8](u1-l4-cli-entry-and-options.md) 重读 `main.cpp` 的完整启动顺序，体会 `enable_utf8_console` → `get_utf8_args` → `parse_options` 这一串为何必须排在最前。
- 若目标是接入一个全新的、当前未覆盖的平台（如 iOS、嵌入式 RTOS），可参考 [u8-l6 二次开发：接入新模型家族](u8-l6-add-new-model.md) 的思路，先判断该平台是否有 ncnn 底层支持，再决定是否需要在 `xmake.lua` 新增 `is_plat(...)` 分支。
