# CLI 入口、选项解析与 UTF-8

## 1. 本讲目标

本讲是「上手与项目全貌」单元的第四篇。前面三讲我们知道了项目是什么、怎么编译、源码放在哪里。这一讲我们要回答一个更具体的问题:

> 当你在终端敲下 `xmake run llm_ncnn_run --model ./assets/qwen3_0.6b` 之后,程序究竟从哪一行开始执行?它怎么知道你想用哪个模型、开不开 Vulkan、用几条线程?

学完本讲,你应当能够:

1. 读懂 `examples/llm_ncnn_run/main.cpp` 的整体流程,说清每一步在做什么。
2. 理解 `Options` 结构体和 `parse_options` 函数的命令行解析机制,并能为它新增一个选项。
3. 理解 `normalize_model_path` 为什么要把裸模型名补成 `./assets/xxx`。
4. 说清 `enable_utf8_console` 与 `get_utf8_args` 解决的 Windows 下中文路径乱码问题。

本讲全部围绕 `llm_ncnn_run` 这个最重要的示例 target 展开。它是绝大多数用户接触项目的第一个入口,也是后续讲义(u2 推理主链路、u7 对话模板)共同的调用方。

## 2. 前置知识

在进入源码之前,先建立几个直觉。

### 2.1 什么是 CLI 程序的「入口」

C++ 程序的入口是 `main` 函数,签名通常是:

```cpp
int main(int argc, char** argv)
```

- `argc`:命令行参数的个数(包括程序名本身)。
- `argv`:一个字符串数组,`argv[0]` 是程序名,`argv[1]`、`argv[2]` …… 是用户传入的参数。

例如执行 `llm_ncnn_run --model ./assets/qwen3_0.6b --threads 4`,则:

| 下标 | 值 |
|------|----|
| `argv[0]` | `llm_ncnn_run` |
| `argv[1]` | `--model` |
| `argv[2]` | `./assets/qwen3_0.6b` |
| `argv[3]` | `--threads` |
| `argv[4]` | `4` |

「选项解析」就是把这一串字符串翻译成程序内部能用的数据(本讲里就是 `Options` 结构体)。

### 2.2 ncnn_llm 的命令行约定

`llm_ncnn_run` 采用一种很常见的「长选项」风格:`--选项名` 后面跟一个值(或不跟值)。它没有用第三方解析库(如 `getopt`),而是手写了一个简单的 `for` 循环逐个匹配字符串。这样做的好处是零依赖、易读、易改,代价是要自己处理「缺值报错」这类细节。本讲会带你完整读一遍这个循环。

### 2.3 为什么要专门讲 UTF-8

ncnn_llm 面向边缘设备与桌面,**经常在 Windows 上运行**。Windows 原生控制台默认使用 ANSI(在中文系统上是 GBK)代码页,而 C++ 标准库的 `argv` 在 Windows 上拿到的就是 ANSI 编码的字符串。如果你的模型路径或提示词里含中文(例如 `--model ./assets/通义千问`),`argv` 里就会是一串被 GBK 编码的字节,C++ 程序按 UTF-8 去理解就会出现乱码甚至找不到文件。`get_utf8_args` 与 `enable_utf8_console` 就是专门用来解决这个痛点的。理解它们,你就理解了为什么 `main` 的第一行不是 `parse_options` 而是 `enable_utf8_console()`。

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `examples/llm_ncnn_run/` 与 `examples/` 下:

| 文件 | 作用 |
|------|------|
| `examples/llm_ncnn_run/main.cpp` | 程序入口 `main`,串联「UTF-8 处理 → 选项解析 → 路径归一化 → 模板检测 → 构造模型 → 进入 run_cli」整条链路 |
| `examples/llm_ncnn_run/options.h` | 定义 `Options` 结构体和 `parse_options` 的声明 |
| `examples/llm_ncnn_run/options.cpp` | `parse_options` 的实现(命令行循环)与 `print_usage` 帮助文本 |
| `examples/utf8_args.h` | `get_utf8_args` / `enable_utf8_console` 的跨平台 UTF-8 处理 |

> ⚠️ **路径提醒**:讲义大纲里把这个文件写作 `examples/llm_ncnn_run/utf8_args.h`,但仓库里它实际位于 `examples/utf8_args.h`(高一层的 `examples/` 目录)。`main.cpp` 里写的是 `#include "utf8_args.h"`,能找到它是因为 [xmake.lua:76](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L76) 给 `llm_ncnn_run` 这个 target 加了 `add_includedirs("examples/")`。`ocr_main.cpp`、`asr_main.cpp` 也共用同一个头文件,所以它被放在了公共的 `examples/` 层。

此外,`main.cpp` 还调用了来自核心库(`ncnn_llm`)和同目录其他文件的能力:

- `run_cli`、`detect_template_type`:来自 `cli_runner.h/cpp`(本讲只看签名,实现细节留到 u2-l5)。
- `make_builtin_tools`、`make_builtin_router`:来自 `tools.h/cpp`(留到 u7-l3)。
- `load_image_to_ncnn_mat`、`ncnn_mat_empty`:来自核心库的图像工具(留到 u5-l2)。
- `ncnn_llm_gpt`:核心模型类,构造它就启动了一次模型加载(留到 u2)。

本讲的策略是:**只盯着「命令行这一层」看透**,把后续模块的细节留给对应讲义。

## 4. 核心概念与源码讲解

### 4.1 main 主流程

#### 4.1.1 概念说明

`main` 是整个程序的「调度员」。它本身不做推理、不做分词,它只负责把用户在命令行表达的需求,翻译成一连串初始化动作,最后把控制权交给 `run_cli`——真正跑对话循环的函数。

可以把 `main` 想象成工厂的「前台」:

1. 先把语言环境理顺(UTF-8)。
2. 把用户的指令(命令行参数)整理成一张「需求单」(`Options`)。
3. 核对需求单上的地址(模型路径)是否真实存在。
4. 根据地址推断这家工厂该用哪套「包装规格」(对话模板)。
5. 把工厂(模型)建起来,准备好工具箱(内置工具)和可能的原材料(图像)。
6. 把这一切移交给生产线(`run_cli`)。

#### 4.1.2 核心流程

`main` 的执行步骤如下:

```text
启动
  │
  ├─ 1. enable_utf8_console()            # 让 Windows 控制台用 UTF-8 收发
  ├─ 2. get_utf8_args(argc, argv)        # 把 argv 转成干净的 UTF-8 字符串
  ├─ 3. 转回 char* 数组 cargv            # parse_options 只认 char**
  ├─ 4. parse_options(cargv)             # 解析命令行 → Options
  ├─ 5. normalize_model_path(opt.model)  # 裸名补成 ./assets/xxx
  ├─ 6. 检查模型路径是否存在              # 不存在直接 return 1
  ├─ 7. detect_template_type(model_path) # 读 model.json → ChatML/YouTu
  ├─ 8. 构造 ncnn_llm_gpt model(...)     # 加载网络与分词器
  ├─ 9. 准备 builtin_tools / router      # 可选的内置工具
  ├─ 10. 若有 --image,加载图像到 ncnn::Mat
  └─ 11. run_cli(opt, model, ...)        # 进入交互式对话循环
```

#### 4.1.3 源码精读

整个 `main` 只有一屏,我们先看它的骨架:

[examples/llm_ncnn_run/main.cpp:26-56](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp#L26-L56) —— `main` 函数全貌,把命令行参数一步步变成一次模型加载和对话循环。

逐段拆开:

```cpp
enable_utf8_console();                                       // 第 27 行:先理顺控制台编码
std::vector<std::string> utf8_args = get_utf8_args(argc, argv); // 第 28 行:拿到干净 UTF-8 参数
```

注意顺序:必须**先**让控制台变成 UTF-8(第 27 行),**再**读取参数(第 28 行)。这两行的原理见 4.4 节。

```cpp
std::vector<char*> cargv;                       // 第 29 行
cargv.reserve(utf8_args.size());
for (auto& s : utf8_args) cargv.push_back(const_cast<char*>(s.c_str())); // 第 31 行
```

这段做了一次「类型适配」:`parse_options` 的接口是 `(int argc, char** argv)`(传统 C 风格),而 `get_utf8_args` 返回的是现代的 `std::vector<std::string>`。这里把每个 `std::string` 的内部缓冲指针(`c_str()`)取出来塞进一个 `char*` 数组。`const_cast` 是为了去掉 `c_str()` 返回的 `const`,因为这些字符串随后只会被「只读地」读取,不会被修改。

```cpp
Options opt = parse_options((int)cargv.size(), cargv.data()); // 第 33 行:解析得到需求单
opt.model_path = normalize_model_path(opt.model_path);        // 第 34 行:补全模型路径
```

第 34 行调用本讲的另一个主角 `normalize_model_path`(见 4.3 节)。

```cpp
if (!std::filesystem::exists(opt.model_path)) {               // 第 36 行:地址核对
    std::cerr << "Model path does not exist: " << opt.model_path << "\n";
    return 1;
}
```

这是「早失败」(fail fast)的好习惯:模型路径不对就立刻退出,避免后面加载时报出晦涩的 ncnn 错误。

```cpp
TemplateType template_type = detect_template_type(opt.model_path); // 第 41 行:推断模板
ncnn_llm_gpt model(opt.model_path, opt.vulkan... );            // 第 43 行:构造模型
std::vector<json> builtin_tools = opt.enable_builtin_tools ? make_builtin_tools() : std::vector<json>(); // 第 44 行
auto builtin_router = make_builtin_router();                   // 第 45 行
```

第 43 行的 `ncnn_llm_gpt` 构造是本讲里最「重」的一步——它会读取 `model.json`、加载 ncnn 网络(`.param`/`.bin`)、初始化分词器。这部分细节属于 u1-l5(配置体系)和 u2(推理主链路),本讲只把它当成「一个构造调用」。

最后是图像(可选)和移交:

```cpp
ncnn::Mat image;
if (!opt.image_path.empty()) {                                 // 第 48 行
    image = load_image_to_ncnn_mat(opt.image_path);
    if (ncnn_mat_empty(image)) { /* 报错并退出 */ }
    std::cerr << "Image loaded: " << opt.image_path << "\n";
}
return run_cli(opt, model, builtin_tools, builtin_router, template_type, image); // 第 56 行
```

`main` 的返回值就是 `run_cli` 的返回值(对话循环正常结束返回 0)。

#### 4.1.4 代码实践

**实践目标**:在不运行模型的前提下,验证你对 `main` 流程顺序的理解。

**操作步骤**:

1. 打开 [main.cpp:26-56](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp#L26-L56)。
2. 在第 36 行的「路径存在性检查」**之前**和**之后**各加一行日志(标注为示例代码):

   ```cpp
   // 示例代码:仅用于观察流程,非项目原有代码
   std::cerr << "[trace] before exists-check, model_path=" << opt.model_path << "\n";
   if (!std::filesystem::exists(opt.model_path)) { ... }
   std::cerr << "[trace] after exists-check, will detect template\n";
   ```

3. 执行 `xmake build llm_ncnn_run`。
4. 故意传一个不存在的模型名运行:`xmake run llm_ncnn_run --model ./assets/__no_such_model__`。

**需要观察的现象**:你会先看到 `before exists-check` 那行,然后是 `Model path does not exist` 报错并退出,**不会**看到 `after exists-check`,更不会进入模板检测。

**预期结果**:这验证了「路径检查」是一道闸门——它失败时,后面的 `detect_template_type` / 模型构造**根本不会被调用**,符合「早失败」的设计。

**待本地验证**:本实践需要在已配置好 xmake 与 ncnn(master)的环境中编译;若暂无环境,可仅做源码阅读并口述预期现象。

#### 4.1.5 小练习与答案

**练习 1**:如果把第 27 行的 `enable_utf8_console()` 删掉,程序在 Linux 上会出问题吗?在 Windows 上呢?

> **答案**:Linux 几乎不受影响,因为 Linux 终端默认就是 UTF-8,`enable_utf8_console` 在非 Windows 平台是空操作(见 4.4 节)。Windows 上则可能导致含中文的 `std::cout` 输出(以及读取含中文的 stdin)出现乱码。

**练习 2**:`main` 把 `std::vector<std::string>` 转回 `char*` 数组(`cargv`)再传给 `parse_options`,为什么不直接让 `parse_options` 接收 `std::vector<std::string>`?

> **答案**:这是为了和「传统 C 风格 `(int argc, char** argv)`」保持一致,方便复用、方便和标准 `main` 签名对接。`parse_options` 内部用 `argv[i]` 下标访问,`char**` 足够;转一道是为了把现代的 UTF-8 字符串和这个传统接口粘合起来。

---

### 4.2 Options 结构与 parse_options

#### 4.2.1 概念说明

`Options` 是「需求单」的数据结构——一个普通的结构体,每个字段对应一个命令行选项。`parse_options` 是「翻译官」——它遍历 `argv`,把字符串形式的选项写进 `Options` 的字段里。

这种「结构体 + 手写循环」是小型 CLI 程序最常见的解析方式,优点是:

- **零依赖**:不引入 `getopt`、`CLI11` 等库。
- **可读**:所有选项和帮助文本都在一个文件里。
- **易改**:加一个选项只需要改 3 处(见 4.2.4 的实践)。

#### 4.2.2 核心流程

`parse_options` 的解析循环逻辑可以用伪代码概括:

```text
新建一个默认的 Options opt        # 字段都有默认值
for i 从 1 到 argc-1:            # 跳过 argv[0](程序名)
    a = argv[i]
    若 a == "--":           跳出循环(后续不再解析)
    若 a == "--help":       打印帮助并 exit(0)
    若 a == "--model":      取下一个 argv 为值,写进 opt.model_path
    若 a == "--image":      取下一个 argv 为值,写进 opt.image_path
    若 a == "--use-vulkan": opt.use_vulkan = true(布尔型,无值)
    若 a == "--vulkan-device": 取下一个 argv,atoi 成 int 写进 opt.vulkan_device
    若 a == "--threads":    取下一个 argv,atoi 成 int 写进 opt.num_threads
    若 a == "--no-builtin-tools": opt.enable_builtin_tools = false(布尔型)
    否则:                   报 "Unknown option" 并 exit(2)
return opt
```

注意两种选项风格的差异:

- **带值选项**(如 `--model`、`--threads`):匹配到关键词后,要把循环下标 `i` 再 `+1`,取出**下一个**参数作为值;还要先检查 `i + 1 < argc`,否则就缺值。
- **标志型选项**(如 `--use-vulkan`、`--no-builtin-tools`):不带值,匹配到就把对应布尔字段置反。

#### 4.2.3 源码精读

先看 `Options` 结构体本身——简洁到只有 5 个字段,每个都有默认值:

[examples/llm_ncnn_run/options.h:5-12](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/options.h#L5-L12) —— `Options` 结构体,字段默认值即「不传选项时的行为」。

```cpp
struct Options {
    std::string model_path = "./assets/qwen3_0.6b"; // 默认模型
    std::string image_path;                          // 默认空 → 不加载图像
    bool use_vulkan = false;                         // 默认 CPU
    bool enable_builtin_tools = true;                // 默认开启内置工具
    int num_threads = 0;   // 0 = use ncnn::get_cpu_count()  ← 注释说明 0 的特殊含义
    int vulkan_device = 0; // Vulkan device index
};
```

注意 `num_threads = 0` 不是「0 条线程」,而是一个「哨兵值」,表示「让 ncnn 自动选 CPU 核数」。这种「特殊默认值 + 注释」的设计在 C++ 里很常见。

再看 `parse_options` 的实现,核心是这一个 `for` 循环:

[examples/llm_ncnn_run/options.cpp:34-79](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/options.cpp#L34-L79) —— 命令行解析主循环,逐个匹配字符串并把值写回 `Options`。

我们看一个「带值选项」的完整处理,以 `--model` 为例:

```cpp
} else if (a == "--model") {            // 第 44 行:匹配关键词
    if (i + 1 >= argc) {                // 第 45 行:先检查是否还有下一个参数
        std::cerr << "Missing value for --model\n";
        std::exit(2);                   // 缺值 → 退出码 2(用法错误)
    }
    opt.model_path = argv[++i];         // 第 49 行:取下一个参数作为值,i 同时前进
}
```

关键是 `argv[++i]`:`++i` 让循环下标跳过这个值,这样下一轮 `for` 的 `++i` 就会指向「值的下一个」参数,而不会把值当成新的选项关键词再解析一遍。

再看一个「标志型选项」——没有值,直接置反布尔字段:

```cpp
} else if (a == "--use-vulkan") {       // 第 56 行
    opt.use_vulkan = true;              // 不取下一个参数
}
```

以及一个「整型值选项」,用 `std::atoi` 把字符串转成 `int`:

```cpp
} else if (a == "--threads") {          // 第 64 行
    if (i + 1 >= argc) { /* 缺值报错 */ }
    opt.num_threads = std::atoi(argv[++i]);  // 第 69 行:字符串 → int
}
```

最后是「兜底」分支——遇到不认识的选项就报错并退出:

```cpp
} else {                                // 第 72 行
    std::cerr << "Unknown option: " << a << "\n";
    print_usage(argv[0]);               // 顺便打印帮助
    std::exit(2);
}
```

帮助文本由 `print_usage` 生成,它列出了所有支持的选项:

[examples/llm_ncnn_run/options.cpp:12-30](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/options.cpp#L12-L30) —— `print_usage`,把所有选项的语义和示例写在一起,既是 `--help` 的输出,也是开发者改选项时的清单。

> 小知识:`--`(单独一个)的作用是「后续参数都不当选项解析」。本讲程序里它直接 `break`(第 38-40 行),实际上 `llm_ncnn_run` 并没有「位置参数」需要它,保留它只是惯例。

#### 4.2.4 代码实践

**实践目标**:亲手给 `Options` 新增一个 `--max-tokens <N>` 选项,跑通「加字段 → 加解析 → 在 main 里使用」的完整闭环。这是本讲的**主实践任务**。

**操作步骤**:

1. **在 `Options` 中加字段**。编辑 [options.h:5-12](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/options.h#L5-L12),在结构体里加一行(示例代码):

   ```cpp
   int max_tokens = 0;  // 0 = 用生成器的默认上限
   ```

2. **在帮助文本里登记**。编辑 `print_usage`(options.cpp 第 12-30 行),在 Options 列表里加一行(示例代码):

   ```cpp
   << "  --max-tokens <num>         Max tokens to generate (default: 0 = unlimited)\n"
   ```

3. **在 `parse_options` 里解析**。仿照 `--threads` 的写法,在 options.cpp 的循环里(第 64-69 行 `--threads` 分支**之后**)插入(示例代码):

   ```cpp
   } else if (a == "--max-tokens") {
       if (i + 1 >= argc) {
           std::cerr << "Missing value for --max-tokens\n";
           std::exit(2);
       }
       opt.max_tokens = std::atoi(argv[++i]);
   }
   ```

4. **在 `main` 里打印它**。编辑 [main.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp#L26-L56),在第 34 行(`normalize_model_path`)之后加一行(示例代码):

   ```cpp
   std::cerr << "max_tokens = " << opt.max_tokens << "\n";
   ```

5. 编译运行:`xmake build llm_ncnn_run`,然后 `xmake run llm_ncnn_run --max-tokens 128 --help`(用 `--help` 验证帮助文本也更新了),或 `xmake run llm_ncnn_run --max-tokens 128 --model ./assets/qwen3_0.6b`。

**需要观察的现象**:

- 带 `--max-tokens 128` 运行时,stderr 应打印 `max_tokens = 128`。
- 不带该选项时,应打印 `max_tokens = 0`(默认值)。
- `--help` 的输出里应出现你新增的那一行。

**预期结果**:这验证了「结构体默认值 → 循环覆盖 → main 消费」三段式解析机制完全打通。

**进阶提示(选做)**:本实践只让 `max_tokens` 在 `main` 里被打印,并未真正限制生成长度。真正限制生成的是 `GenerateConfig::max_new_tokens`(见 [src/ncnn_llm_gpt.h:33](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L33),默认 `4096`)。注意它叫 `max_new_tokens` 而非 `max_tokens`。若想把命令行选项真正接进生成,需要在 [cli_runner.cpp:71-75](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L71-L75) 构造 `GenerateConfig cfg;` 之后加一句 `if (opt.max_tokens > 0) cfg.max_new_tokens = opt.max_tokens;`。这部分留到 u2-l4(解码循环)再深入。

**待本地验证**:若暂无编译环境,可只完成源码修改并在笔记里推演 `argv` 的变化。

#### 4.2.5 小练习与答案

**练习 1**:如果用户写成 `--threads`(后面没有跟数字)就回车,程序会怎样?

> **答案**:第 65 行的 `if (i + 1 >= argc)` 会成立,程序打印 `Missing value for --threads` 并以退出码 2 退出,不会去读越界的 `argv[i+1]`。

**练习 2**:为什么 `parse_options` 对未知选项选择 `exit(2)` 而不是 `exit(0)`?

> **答案**:退出码 0 表示成功,非 0 表示失败。Unix 惯例里「用法错误」(usage error)常用 `2`(`1` 多用于一般性运行失败)。这样脚本可以通过 `$?` 区分「成功」「运行失败」「参数错误」。

**练习 3**:`std::atoi("abc")` 会返回什么?这会给 `--threads abc` 带来什么隐患?

> **答案**:`std::atoi` 解析失败时返回 `0`,且**不会报错**。所以 `--threads abc` 会悄悄地把 `num_threads` 设成 `0`(即「自动选核数」),用户可能察觉不到自己写错了。更稳健的做法是用 `util.h` 里的 `parse_int`(返回 `std::optional<int>`,解析失败返回空),但当前 `parse_options` 为简洁起见用了 `atoi`。

---

### 4.3 normalize_model_path 模型路径归一化

#### 4.3.1 概念说明

用户在命令行指定模型时,可能有三种写法:

1. 完整路径:`--model /home/user/assets/qwen3_0.6b`
2. 带目录的相对路径:`--model ./assets/qwen3_0.6b`
3. **只写模型名**:`--model qwen3_0.6b`(最省事的写法)

`normalize_model_path` 的职责就是:当用户用第 3 种「裸名」写法时,自动把它补成 `./assets/qwen3_0.6b`,这样用户不必每次都写长长的 `./assets/` 前缀。这是「为易用性做的一层小翻译」。

#### 4.3.2 核心流程

```text
输入: path
  │
  ├─ path 是绝对路径(以 / 开头,或 Windows 下带盘符)?
  │     是 → 原样返回
  │
  ├─ path 没有 parent_path(即只是一个裸文件名,没有目录分隔符)?
  │     是 → 返回 "./assets/" + path
  │
  └─ 其他(已经是 ./xxx 或 a/b 形式)
        → 原样返回
```

判断依据来自 C++17 的 `<filesystem>`:

- `p.is_absolute()`:是否绝对路径。
- `p.has_parent_path()`:是否包含目录部分。`qwen3_0.6b` 没有 parent_path,而 `./assets/qwen3_0.6b` 有。

#### 4.3.3 源码精读

这个函数很短,藏在 `main.cpp` 的匿名命名空间里:

[examples/llm_ncnn_run/main.cpp:15-22](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp#L15-L22) —— `normalize_model_path`,把裸模型名补全为 `./assets/` 下的路径。

```cpp
std::string normalize_model_path(std::string path) {
    std::filesystem::path p(path);
    if (p.is_absolute()) return path;          // 情况 1:绝对路径,不动
    if (!p.has_parent_path()) {                // 情况 3:裸名
        return (std::filesystem::path("./assets") / p).string();  // 拼成 ./assets/裸名
    }
    return path;                               // 情况 2:已带目录,不动
}
```

注意 `path("./assets") / p` 用的是 `filesystem` 的 `/` 运算符来拼接路径,它会自动处理分隔符(跨平台友好),比手动 `string + "/" + string` 更稳健。

它被 `main` 第 34 行调用:

```cpp
opt.model_path = normalize_model_path(opt.model_path);
```

这一步发生在「存在性检查」之前,所以接下来第 36 行检查的就是**归一化后**的路径。

> 为什么默认前缀是 `./assets/`?因为 u1-l3 讲过,`assets/` 是约定俗成的「放模型权重」的目录(权重需自行下载)。`Options::model_path` 的默认值 `"./assets/qwen3_0.6b"` 也印证了这一点。

#### 4.3.4 代码实践

**实践目标**:理解 `normalize_model_path` 的三种分支,并能预测输出。

**操作步骤**(纯源码阅读型实践,无需编译):

1. 打开 [main.cpp:15-22](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp#L15-L22)。
2. 在笔记里画出下面 4 个输入分别会命中哪个分支、返回什么:

   | 输入 `path` | 命中分支 | 返回值 |
   |------------|---------|--------|
   | `qwen3_0.6b` | ? | ? |
   | `./assets/qwen3_0.6b` | ? | ? |
   | `/opt/models/qwen3_0.6b` | ? | ? |
   | `assets/qwen3_0.6b`(注意没有 `./`) | ? | ? |

3. **预期答案**(对照确认):

   | 输入 | 命中分支 | 返回值 |
   |------|---------|--------|
   | `qwen3_0.6b` | 裸名分支(`!has_parent_path`) | `./assets/qwen3_0.6b` |
   | `./assets/qwen3_0.6b` | 最后的「原样返回」(已有 parent_path `./assets`) | `./assets/qwen3_0.6b` |
   | `/opt/models/qwen3_0.6b` | 绝对路径分支 | `/opt/models/qwen3_0.6b` |
   | `assets/qwen3_0.6b` | 最后的「原样返回」(`has_parent_path()` 为真,因为 parent 是 `assets`) | `assets/qwen3_0.6b` |

**需要观察的现象**:第 4 行是个**陷阱**——`assets/qwen3_0.6b`(不带 `./`)会被当成「已带目录」而原样返回,而不是补成 `./assets/assets/qwen3_0.6b`。这正是 `has_parent_path()` 的判定结果。

**预期结果**:理解「裸名」的精确定义是「没有任何目录分隔符的纯名字」,而不是「不以 `./` 开头」。

#### 4.3.5 小练习与答案

**练习 1**:如果用户输入 `--model .`(一个点),`normalize_model_path` 会返回什么?这会造成什么后果?

> **答案**:`.` 是一个「名字」,`p.has_parent_path()` 对 `.` 为假(它没有 parent),所以会走裸名分支,返回 `./assets/.`。随后 `main` 第 36 行的存在性检查会发现 `./assets/.`(即 `assets/` 目录本身)存在,于是不会报错,但接下来 `ncnn_llm_gpt` 构造会去 `assets/.` 里找 `model.json` 而失败。这说明 `normalize_model_path` 只做机械的字符串归一化,不校验语义。

**练习 2**:为什么这个函数放在匿名命名空间(`namespace { ... }`)里?

> **答案**:匿名命名空间把函数的可见性限制在本编译单元(`main.cpp`)内,相当于 `static`。`normalize_model_path` 只是 `main` 的私有助手,不需要暴露给其他文件,放匿名命名空间可以避免链接期符号冲突。

---

### 4.4 UTF-8 控制台/参数处理

#### 4.4.1 概念说明

这是本讲最容易让 Windows 用户踩坑、也最体现「跨平台工程经验」的一块。

**问题根源**:C++ 标准的 `main(int, char**)` 在 Windows 上拿到的 `argv`,是按系统当前 **ANSI 代码页**(中文系统通常是 GBK / CP936)编码的字节流。而 ncnn_llm 内部一切字符串(模型路径里的中文、分词器的词表、用户提示词)都按 **UTF-8** 处理。两套编码不一致,就会出现:

- 命令行传中文模型路径 → `argv` 里是 GBK 字节 → 程序按 UTF-8 理解 → 乱码 → `std::filesystem::exists` 找不到文件。
- `std::cout` 打印中文 → 控制台按 GBK 解释 UTF-8 字节 → 屏幕乱码。

`get_utf8_args` 和 `enable_utf8_console` 分别解决这两个方向:

- `get_utf8_args`:**输入方向**——从 Windows 的宽字符命令行重新拿到真正的 UTF-8 参数。
- `enable_utf8_console`:**输出方向**——把控制台的输入输出代码页都设成 UTF-8。

而在 Linux/macOS 上,这两个函数都是**空操作**(no-op),因为那些平台默认就是 UTF-8。这就是「跨平台抽象」的典型写法:用宏 `#ifdef _WIN32` 把平台差异封装在一个头文件里,上层代码(`main`)只调用统一接口。

#### 4.4.2 核心流程

`get_utf8_args` 的跨平台逻辑:

```text
输入: argc, argv
  │
  ├─ Windows(_WIN32):
  │     CommandLineToArgvW(GetCommandLineW())   # 拿宽字符 argv (wargv)
  │     for 每个 wargv[i]:
  │         WideCharToMultiByte(CP_UTF8, wargv[i])  # 宽字符 → UTF-8 字节
  │     返回 UTF-8 字符串数组
  │
  └─ 非 Windows:
        for 每个 argv[i]: 直接拷贝   # 本来就是 UTF-8
        返回
```

`enable_utf8_console` 的逻辑:

```text
  ├─ Windows: SetConsoleOutputCP(CP_UTF8); SetConsoleCP(CP_UTF8);
  └─ 非 Windows: 什么都不做
```

#### 4.4.3 源码精读

整个跨平台方案集中在一个头文件里(注意它在 `examples/utf8_args.h`,不是 `examples/llm_ncnn_run/` 下):

[examples/utf8_args.h:19-39](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/utf8_args.h#L19-L39) —— `get_utf8_args`,Windows 下从宽字符命令行恢复 UTF-8 参数,其他平台直接拷贝。

Windows 分支的核心是两个 Win32 API:

```cpp
LPWSTR* wargv = CommandLineToArgvW(GetCommandLineW(), &wargc);  // 第 23 行:拿宽字符 argv
...
int n = WideCharToMultiByte(CP_UTF8, 0, wargv[i], -1, nullptr, 0, nullptr, nullptr); // 第 26 行:先算长度
std::string s(n > 0 ? n - 1 : 0, '\0');                          // 第 27 行:开好缓冲
WideCharToMultiByte(CP_UTF8, 0, wargv[i], -1, &s[0], n, ...);   // 第 29 行:真正转换
```

这是一个「两遍调用」的惯用法:`WideCharToMultiByte` 第一次传 `nullptr` 缓冲只是为了**求出需要的字节数 `n`**,第二次才真正写入。`n-1` 是因为返回值包含了结尾的 `\0`,而 `std::string` 不需要它。

非 Windows 分支极简——直接拷贝:

```cpp
for (int i = 0; i < argc; i++) args.push_back(argv[i]);  // 第 37 行:Linux/macOS 默认 UTF-8
```

输出方向的 `enable_utf8_console` 同样简洁:

[examples/utf8_args.h:43-48](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/utf8_args.h#L43-L48) —— `enable_utf8_console`,把 Windows 控制台切到 UTF-8,非 Windows 为空操作。

```cpp
inline void enable_utf8_console() {
#ifdef _WIN32
    SetConsoleOutputCP(CP_UTF8);   // 输出代码页 → UTF-8
    SetConsoleCP(CP_UTF8);         // 输入代码页 → UTF-8
#endif
}
```

回到 `main`,它们的调用顺序很关键:

```cpp
enable_utf8_console();                                      // main.cpp 第 27 行
std::vector<std::string> utf8_args = get_utf8_args(argc, argv); // main.cpp 第 28 行
```

先「设好控制台代码页」,再「读参数」,保证后续所有 `std::cerr`/`std::cout`(比如第 37 行的报错、第 54 行的 `Image loaded`)都能正确显示中文。

> 头文件注释里还点出一个细节:`enable_utf8_console` 在 Git Bash 这类 **pty**(伪终端)环境下其实是 no-op,因为 pty 本身已经处理了编码;它真正起作用的是 `cmd` / PowerShell 这类原生 Windows 控制台。

#### 4.4.4 代码实践

**实践目标**:理解 `get_utf8_args` 在不同平台的分支,并能解释它解决了什么问题(本讲主实践任务的后半问)。

**操作步骤**:

1. 阅读 [utf8_args.h:19-39](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/utf8_args.h#L19-L39) 与 [utf8_args.h:43-48](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/utf8_args.h#L43-L48)。
2. 在笔记里用一段话回答:**`enable_utf8_console` 与 `get_utf8_args` 各自解决了什么问题?为什么 `main` 里要先调前者再调后者?**

**参考答案要点**:

- `enable_utf8_console`:解决**输出/输入到控制台**的编码问题——让 `cmd`/PowerShell 的输入输出代码页变成 UTF-8,从而 `std::cout` 打印中文不乱码、`std::getline(std::cin, ...)` 读取中文提示词不乱码。
- `get_utf8_args`:解决**命令行参数本身**的编码问题——Windows 原生 `argv` 是 ANSI(GBK)编码,它从宽字符命令行重新提取出 UTF-8 字节,使含中文的 `--model ./assets/通义千问` 能被正确解析。
- 先后顺序:先 `enable_utf8_console` 把控制台切到 UTF-8,这样即使后面 `get_utf8_args` 之后的某一步要打印诊断信息(含中文路径),也能正确显示。

3. (可选,需 Windows 环境)在 Windows 上构建后,分别用 `--model ./assets/中文模型名` 测试:不调用 `get_utf8_args`(临时把 main 第 28 行改成直接用 `argv`)时是否会出现「路径不存在」的误报。

**需要观察的现象**:在 Windows + 中文路径下,启用 UTF-8 处理能正确找到模型;禁用则可能误报不存在。

**预期结果**:验证两个函数分别覆盖「控制台 I/O」和「argv 编码」两个方向,缺一不可。

**待本地验证**:Linux 上无法复现 GBK 问题,此项需在 Windows(中文系统)环境验证。

#### 4.4.5 小练习与答案

**练习 1**:`get_utf8_args` 里 `WideCharToMultiByte` 为什么要调用两次?

> **答案**:第一次(传 `nullptr` 缓冲)是为了**查询目标 UTF-8 字符串需要多少字节**,因为宽字符→UTF-8 的长度无法简单由宽字符个数推算(UTF-8 是变长编码)。拿到长度 `n` 后开好 `std::string` 缓冲,第二次才真正把字节写进去。这是 Win32 字符集转换的标准惯用法。

**练习 2**:为什么这两个函数都用 `inline` 定义在头文件里,而不是声明在 `.h`、实现在 `.cpp`?

> **答案**:因为它们被 `#ifdef _WIN32` 包裹的平台分支实现,且要被多个示例(`main.cpp`、`ocr_main.cpp`、`asr_main.cpp`)共用。定义成 `inline` 放头文件,可以「一处包含、处处可用」,且不会违反「一次定义规则」(ODR)。对这么小的函数,`inline` 还能避免一次函数调用开销。

**练习 3**:如果在 Linux 上删除 `enable_utf8_console()` 这一行调用,程序行为会变吗?

> **答案**:不会。`enable_utf8_console` 在非 `_WIN32` 平台是空函数体,删掉它的调用对 Linux 行为毫无影响——这也正是跨平台抽象的目的:上层代码写一份,平台差异被宏隔离在下层。

---

## 5. 综合实践

把本讲四个模块串起来,完成下面这个贯通任务。

**任务**:为 `llm_ncnn_run` 增加一个 `--verbose` 标志型选项(不带值),开启后在 `main` 的每个关键步骤打印一行诊断日志,并确保含中文的模型路径在 Windows 下也不会乱码。

**要求**:

1. 在 `Options` 里加 `bool verbose = false;`。
2. 在 `parse_options` 里解析 `--verbose`(参考 `--use-vulkan` 的标志型写法),并在 `print_usage` 登记。
3. 在 `main` 里,当 `opt.verbose` 为真时,依次打印:`[verbose] model_path=...`(归一化后)、`[verbose] template=ChatML/YouTu`、`[verbose] vulkan=<0/1> threads=...`。
4. 解释:为什么即便加了 `--verbose`,你的日志在 Windows 的 cmd 里也能正确显示中文模型名?(提示:回顾 4.4 节 `enable_utf8_console` 的调用时机。)

**验收标准**:

- `xmake run llm_ncnn_run --verbose --model qwen3_0.6b`(裸名)能打印出归一化后的 `./assets/qwen3_0.6b`。
- 不加 `--verbose` 时完全静默(除原有输出外不多打)。
- 能口述「裸名 → `./assets/` 前缀」「argv UTF-8 化」两步分别由哪个函数负责。

**提示**:`opt.verbose` 需要透传进 `run_cli` 吗?本任务不需要——因为诊断日志只在 `main` 阶段打印。但如果想让对话循环里也受 `--verbose` 控制,就要让 `run_cli` 也读到它,这涉及把 `Options` 透传(目前 `run_cli` 已经按 `const Options& opt` 接收,见 [cli_runner.h:18](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.h#L18))。

## 6. 本讲小结

- `main` 是「调度员」,按 **UTF-8 处理 → 选项解析 → 路径归一化 → 存在性检查 → 模板检测 → 构造模型 → 准备工具/图像 → run_cli** 的固定顺序串联整个启动流程。
- `Options` 是命令行选项的数据结构,每个字段对应一个选项并有默认值;`parse_options` 用一个 `for` 循环逐个匹配字符串,区分「带值选项」(用 `argv[++i]` 取值)与「标志型选项」(直接置反布尔)。
- `normalize_model_path` 利用 `<filesystem>` 的 `is_absolute` / `has_parent_path` 判断,把「裸模型名」自动补成 `./assets/裸名`,让用户少打字。
- `enable_utf8_console` / `get_utf8_args` 用 `#ifdef _WIN32` 把 Windows 的 GBK 编码痛点封装起来:前者解决控制台 I/O 编码,后者把 `argv` 从 ANSI 恢复成 UTF-8;在 Linux/macOS 上两者都是空操作。
- 给程序新增一个命令行选项只需要改 **3 处**:`Options` 加字段、`parse_options` 加分支、`print_usage` 加帮助行——这是手写 CLI 解析器最大的好处。
- 命令行层的 `max_tokens` 与生成层的 `max_new_tokens`(`GenerateConfig`,默认 4096)是两回事:前者本讲只做演示,真正限制生成长度的是后者(留待 u2-l4)。

## 7. 下一步学习建议

本讲只讲了「命令行这一层」,把模型构造当成了黑盒。接下来应当:

1. **u1-l5(模型目录与 model.json 配置体系)**:打开 `main` 第 43 行那个 `ncnn_llm_gpt` 构造调用,看它如何读取 `model.json` 的 `params`/`tokenizer`/`setting` 字段——这是理解「模型怎么被加载」的关键。
2. **u2-l1(基类 ncnn_llm_base 与公共能力)**:看构造模型时 `use_vulkan`、`num_threads` 这些 `Options` 字段如何通过 `create_option` 下发给 ncnn。
3. **u7-l1(对话模板)**:本讲第 41 行的 `detect_template_type` 只返回一个枚举,真正的模板拼接(`apply_chat_template`)在 `cli_runner.cpp` 里,届时会展开 ChatML / YouTu 的差异。

建议在进入 u2 之前,先把本讲的「新增 `--max-tokens` 选项」实践亲手做一遍,确保你完全掌握了「命令行字符串 → `Options` 字段 → `main` 消费」这条链路——它是后续所有命令行扩展的基础。
