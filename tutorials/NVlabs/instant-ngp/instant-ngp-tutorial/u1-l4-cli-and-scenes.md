# 命令行运行与示例场景

## 1. 本讲目标

在前几讲里，我们已经认识了 instant-ngp 是什么、目录如何组织、怎么用 CMake 编译。编译成功后，你会得到一个叫 `instant-ngp` 的可执行文件。本讲要回答一个最实际的问题：**拿到这个可执行文件后，我该敲什么命令把它跑起来？**

学完本讲，你应当能够：

1. 读懂 `src/main.cu` 里 `main_func` 的启动流程：参数解析 → 加载文件 → 初始化 → 进入帧循环。
2. 列出 `instant-ngp` 支持的所有命令行参数及其含义。
3. 理解场景文件是如何「自动决定」Testbed 进入哪种模式（NeRF / SDF / Image / Volume）的。
4. 知道 `--no-gui` 无头（headless）模式下，程序如何在命令行报告训练进度。
5. 会用命令行加载四种基元的官方示例数据。

## 2. 前置知识

- **可执行文件与命令行参数**：在终端里运行 `./instant-ngp data/nerf/fox` 时，`./instant-ngp` 是程序，后面的 `data/nerf/fox` 是传给程序的「参数」（argument）。程序通过读取这些参数决定自己要做什么。
- **位置参数 vs 选项参数**：像 `data/nerf/fox` 这样直接跟在程序后面的叫「位置参数」（positional）；像 `--scene fox` 这样带前缀的叫「选项」（option/flag）。
- **ETestbedMode**：这是 instant-ngp 里一个枚举类型，取值有 `Nerf / Sdf / Image / Volume / None`，分别对应四种神经图形基元。本讲不深入它，只要知道程序会根据你给的场景自动选定其中一个模式即可（详见第 4.4 节）。
- **frame（帧）循环**：图形程序通常用一个「不停地循环」的主循环来驱动渲染与训练，每一轮叫一帧。`frame()` 函数返回 `true` 就继续循环，返回 `false` 就退出。

本讲是纯命令行与流程层面的讲解，**不需要 GPU 也能读懂**；动手运行部分需要你已经按 u1-l3 完成编译。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| `src/main.cu` | 程序真正的入口。包含 C 语言入口 `main()` 和逻辑入口 `main_func()`，完成参数解析与启动流程。 |
| `src/testbed.cu` | Testbed 的实现。本讲只用到其中的 `frame()`（帧循环）和 `load_file()`（文件加载分发）。 |
| `include/neural-graphics-primitives/common.h` | 定义 `ETestbedMode` 枚举（四种模式 + None）。 |
| `src/common_host.cu` | 定义 `mode_from_scene()`——根据场景路径自动推断模式的函数。 |
| `scripts/run.py` | 官方提供的 Python 脚本，是命令行的「超集」替代品，本讲末尾做对比。 |
| `README.md` | 官方文档，给出四种基元的运行命令与示例数据说明。 |

## 4. 核心概念与源码讲解

### 4.1 程序入口：从 `main()` 到 `main_func()`

#### 4.1.1 概念说明

一个 C/C++ 程序的执行起点是 `main()` 函数。instant-ngp 的 `main()` 很薄：它只负责把操作系统传进来的命令行参数（`argc` / `argv`）收集成一个 `std::vector<std::string>`，然后转交给真正干活的 `main_func()`。这样做的好处是把「跨平台参数处理」和「业务逻辑」分开，方便测试与复用。

#### 4.1.2 核心流程

```
操作系统启动进程
   │
   ▼
main(argc, argv)            ← C 入口，仅做平台适配与参数收集
   │  把 argv[i] 逐个塞进 vector<string>
   ▼
ngp::main_func(arguments)   ← 真正的业务逻辑入口
   │
   ├─ 解析命令行参数
   ├─ 创建 Testbed 对象
   ├─ 加载文件 / 场景 / 快照 / 网络配置
   ├─ 初始化 GUI（若启用）
   └─ 进入 while (testbed.frame()) 循环
```

#### 4.1.3 源码精读

C 语言入口在这里，Windows 用宽字符版 `wmain`，其他平台用标准 `main`：

[src/main.cu:L195-L216](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L195-L216) —— 把 `argv` 收集成 `vector`，调用 `ngp::main_func(arguments)`，并用 `try/catch` 兜底捕获未处理异常。

关键片段（去掉平台宏后）就是：

```cpp
int main(int argc, char* argv[]) {
    try {
        std::vector<std::string> arguments;
        for (int i = 0; i < argc; ++i) {
            arguments.emplace_back(argv[i]);
        }
        return ngp::main_func(arguments);
    } catch (const exception& e) {
        tlog::error() << fmt::format("Uncaught exception: {}", e.what());
        return 1;
    }
}
```

注意 `arguments[0]` 是程序自身的名字（如 `./instant-ngp`），`main_func` 在解析时会跳过它。

#### 4.1.4 代码实践

1. **实践目标**：确认程序入口与异常兜底行为。
2. **操作步骤**：打开 `src/main.cu`，定位第 199 行的 `main` 与第 29 行的 `main_func`。
3. **观察现象**：注意 `main_func` 被包在 `try` 块里，任何未被内层捕获的 `exception` 都会在这里被打印并返回 1。
4. **预期结果**：你能用一句话说出「`main` 负责 argv 收集 + 异常兜底，`main_func` 负责真正的解析与启动」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 instant-ngp 要把逻辑放在 `main_func` 里，而不是直接写在 `main` 里？

**参考答案**：为了让业务逻辑与平台相关的入口（`wmain`/`main`、UTF-16 转码、异常兜底）解耦。这样 `main_func` 接收的是一个干净的 `vector<string>`，便于测试，也便于在不同平台入口里复用同一段逻辑。

---

### 4.2 命令行参数解析（完整清单）

#### 4.2.1 概念说明

instant-ngp 用一个第三方头文件库 `args.hxx`（位于 `dependencies/args/`）来解析命令行参数。它的用法是：为每一个想支持的参数声明一个对象（`ValueFlag`、`Flag`、`PositionalList` 等），把它们注册到一个 `ArgumentParser`，然后调用 `parser.ParseArgs(...)` 一次性完成解析。之后通过判断这些对象是否「被设置过」来读取用户输入。

参数对象分三类：

- `Flag`：开关型，只有「给了 / 没给」两种状态，如 `--no-gui`。
- `ValueFlag<T>`：带值型，需要跟一个值，如 `--scene fox`、`--width 1920`。
- `PositionalList<string>`：位置参数，不带前缀，按出现顺序收集，如 `./instant-ngp data/nerf/fox`。

#### 4.2.2 核心流程

```
声明所有 Flag / ValueFlag / PositionalList
        │
        ▼
parser.ParseArgs(arguments)   ← 一次性解析
        │
   ┌────┴────────┬───────────┬──────────────┐
   ▼             ▼           ▼              ▼
 抛 Help      抛 ParseError 抛 ValidationError  正常继续
 (打印帮助)   (打印错误)    (打印错误)
```

三种异常分别对应不同返回码（0 / -1 / -2），帮助信息则是正常打印后返回 0。

#### 4.2.3 源码精读

解析器的构造与版本号字符串在这里：

[src/main.cu:L29-L34](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L29-L34) —— 构造 `ArgumentParser`，标题里嵌入了编译期宏 `NGP_VERSION`。

下面是完整的参数清单（按源码声明顺序）。每个参数都对应 [src/main.cu:L36-L117](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L36-L117) 中的一个对象：

| 参数（别名） | 类型 | 含义 |
| :--- | :--- | :--- |
| `-h`, `--help` | Flag | 打印帮助菜单 |
| `-m`, `--mode` | ValueFlag\<string\ | **已废弃**，无任何效果（模式自动判定） |
| `-n`, `-c`, `--network`, `--config` | ValueFlag\<string\ | 网络配置文件路径；不指定则用场景默认 |
| `--no-gui` | Flag | 关闭 GUI，改为在命令行报告训练进度 |
| `--vr` | Flag | 启用 VR |
| `--no-train` | Flag | 启动时不自动开始训练 |
| `-s`, `--scene` | ValueFlag\<string\ | 要加载的场景（NeRF 数据集 / obj·stl 网格 / 图片 / nvdb 体素） |
| `--snapshot`, `--load_snapshot` | ValueFlag\<string\ | 启动时加载的快照（.ingp / .msgpack） |
| `--width` | ValueFlag\<uint32_t\ | GUI 窗口宽度 |
| `--height` | ValueFlag\<uint32_t\ | GUI 窗口高度 |
| `-v`, `--version` | Flag | 打印版本号 |
| `files`（位置参数） | PositionalList\<string\ | 待加载文件，可同时给多个（场景 / 配置 / 快照 / 相机路径） |

解析与异常处理在这里：

[src/main.cu:L121-L149](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L121-L149) —— 解析参数；捕获 `Help`（打印帮助返回 0）、`ParseError`（返回 -1）、`ValidationError`（返回 -2）；随后处理 `--version` 与已废弃的 `--mode`（后者只打印一句警告）。

一个值得注意的细节：`--mode` 被显式标注为 deprecated，源码注释写着 `"Deprecated. Do not use."`，运行时若给了这个参数只会打印一条警告。这印证了 u1-l1 讲过的一点——**模式是由场景自动决定的，不需要用户指定**。

#### 4.2.4 代码实践

1. **实践目标**：亲手整理出一份完整的参数清单，并验证「模式无需指定」。
2. **操作步骤**：
   - 在 `src/main.cu` 第 36–117 行逐行找出所有 `HelpFlag / ValueFlag / Flag / PositionalList` 声明，填入上表。
   - 编译后运行 `./instant-ngp --help`，对比终端打印的帮助文本与你整理的清单是否一致。
   - 运行 `./instant-ngp --version`，确认输出 `Instant Neural Graphics Primitives v<版本号>`（对应 [src/main.cu:L142-L145](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L142-L145)）。
3. **观察现象**：`--help` 列出的每个参数旁边都有大写的「 metavar 」（如 `SCENE`、`WIDTH`），这正是 args.hxx 用 `ValueFlag` 第一个字符串参数设定的占位名。
4. **预期结果**：你能不看源码，仅凭 `--help` 输出说出每个参数的作用；并确认没有任何一个参数需要你显式指定「NeRF/SDF/Image/Volume」。
5. 若本机未编译，则步骤改为纯阅读：上述清单即为参考答案，标注「待本地验证」运行结果。

#### 4.2.5 小练习与答案

**练习 1**：`--network` 和 `-c` 是同一个参数吗？`--scene` 和位置参数 `files` 有什么区别？

**参考答案**：是的，`-n`、`-c`、`--network`、`--config` 都绑定到同一个 `network_config_flag` 对象（[src/main.cu:L50-L55](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L50-L55)）。区别在于：`--scene` 走 `load_training_data()` 专门加载训练数据；而位置参数 `files` 走 `load_file()`，它会根据文件类型自动判别（可能是训练数据、网络配置、快照或相机路径）。详见 4.3 节。

**练习 2**：如果用户同时传了非法参数，程序返回码是多少？从哪里能看出来？

**参考答案**：解析错误返回 -1，校验错误返回 -2，分别在 `catch (const ParseError&)` 和 `catch (const ValidationError&)` 分支里 `return`（[src/main.cu:L132-L139](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L132-L139)）。

---

### 4.3 启动流程与 `frame()` 主循环（含 `--no-gui` 进度报告）

#### 4.3.1 概念说明

参数解析完之后，程序进入「启动阶段」：构造 Testbed 对象、加载用户指定的文件、按需初始化 GUI/VR，然后进入一个无限循环 `while (testbed.frame())`。这个循环就是整个程序的「心脏」——每一圈就是一帧，训练和渲染都在帧循环里发生（帧循环内部细节是 u2-l2 的主题，本讲只关心它如何被启动、何时退出）。

`--no-gui` 的意义在于：在没有显示器的服务器、容器或 Colab 里，你无法打开图形窗口，但仍想训练或评测。这时关闭 GUI，程序改为在命令行不断打印 `iteration=... loss=...` 来汇报进度。

#### 4.3.2 核心流程

```
参数解析完成
   │
   ▼
Testbed testbed;                              ← 创建中枢对象
   │
   ├─ for file in 位置参数:  testbed.load_file(file)
   ├─ if --scene:            testbed.load_training_data(scene)
   ├─ if --snapshot:         testbed.load_snapshot(...)
   │  else if --network:     testbed.reload_network_from_file(...)
   ├─ testbed.m_train = !--no-train
   ├─ if GUI:                testbed.init_window(width ?:1920, height ?:1080)
   ├─ if --vr:               testbed.init_vr()
   │
   ▼
while (testbed.frame()) {                     ← 主循环
   if (!gui) 打印 iteration / loss            ← --no-gui 的进度报告
}
   │
   ▼  frame() 返回 false 时退出（窗口被关闭）
程序结束
```

#### 4.3.3 源码精读

启动阶段的核心代码：

[src/main.cu:L151-L188](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L151-L188) —— 构造 Testbed，依次处理位置参数文件、`--scene`、`--snapshot`/`--network`、训练开关、GUI/VR 初始化，最后进入帧循环。

精简后的关键片段：

```cpp
Testbed testbed;

for (auto file : get(files)) {
    testbed.load_file(file);              // 位置参数：交给 load_file 自动判别
}

if (scene_flag) {
    testbed.load_training_data(get(scene_flag));   // --scene：明确当训练数据加载
}

if (snapshot_flag) {
    testbed.load_snapshot(get(snapshot_flag));
} else if (network_config_flag) {
    testbed.reload_network_from_file(get(network_config_flag));
}

testbed.m_train = !no_train_flag;          // --no-train 控制是否启动即训练

#ifdef NGP_GUI
    bool gui = !no_gui_flag;               // --no-gui 仅在编译了 GUI 时才有效
#else
    bool gui = false;
#endif

if (gui) {
    testbed.init_window(width_flag ? get(width_flag) : 1920,
                        height_flag ? get(height_flag) : 1080);
}

// 渲染/训练主循环
while (testbed.frame()) {
    if (!gui) {
        tlog::info() << "iteration=" << testbed.m_training_step
                     << " loss=" << testbed.m_loss_scalar.val();
    }
}
```

这里有两个要点：

1. **`--no-gui` 受编译宏 `NGP_GUI` 约束**（[src/main.cu:L169-L173](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L169-L173)）。如果你按 u1-l3 用 `-DNGP_BUILD_WITH_GUI=off` 编译，那么 `NGP_GUI` 未定义，`gui` 永远为 `false`——即使你不加 `--no-gui`，程序也是无头的。这与 README FAQ 里「用 `cmake -DNGP_BUILD_WITH_GUI=off` 编译即可无头运行」的说法一致。
2. **进度报告只在无 GUI 时打印**（[src/main.cu:L184-L188](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L184-L188)）。`tlog::info()` 是项目用的日志库 tinylogger，`m_training_step` 是当前训练步数，`m_loss_scalar.val()` 是当前损失值。

`frame()` 何时返回 `false`？看它的实现开头：

[src/testbed.cu:L3908-L3918](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3908-L3918) —— 仅在编译了 GUI 且拥有窗口时调用 `begin_frame()`；若窗口被关闭，`begin_frame()` 返回 false，`frame()` 立即返回 false，循环退出。

函数末尾正常情况下返回 `true`：

[src/testbed.cu:L4033](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4033) —— 一帧正常结束时返回 true，让循环继续。

由此得到一个重要结论：**在 `--no-gui` 模式下没有窗口，`frame()` 永远返回 true，`while` 循环不会自己结束**。所以 `./instant-ngp --no-gui` 会一直训练下去，直到你按 `Ctrl+C` 中断。若想让它「训完自动停」，请改用 `scripts/run.py --n_steps <步数>`（见 4.4.4 与综合实践）。

#### 4.3.4 代码实践

1. **实践目标**：理解「有 GUI / 无 GUI」两条执行路径的差异。
2. **操作步骤**：
   - 在 `src/main.cu` 中找到 `#ifdef NGP_GUI`（第 169 行）和 `while (testbed.frame())`（第 184 行）。
   - 假设两种编译情况：(a) 编译了 GUI 且不加 `--no-gui`；(b) 用 `-DNGP_BUILD_WITH_GUI=off` 编译。
3. **观察现象**：
   - 情况 (a)：程序打开窗口，`frame()` 在你关窗时返回 false 退出；命令行不打印 iteration/loss（因为 `gui` 为 true）。
   - 情况 (b)：`gui` 恒为 false，程序不开窗、不断打印 `iteration=... loss=...`，且不会自动退出。
4. **预期结果**：你能解释「为什么无头模式下程序不会自己停」——因为 `frame()` 只在窗口关闭时才返回 false，而无头模式根本没有窗口。
5. 待本地验证：实际运行行为依赖具体 GPU 与编译选项。

#### 4.3.5 小练习与答案

**练习 1**：`--no-train` 和 `--no-gui` 各自影响什么？

**参考答案**：`--no-train` 只关闭「启动即训练」，把 `testbed.m_train` 置为 false（[src/main.cu:L167](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L167)），程序仍可以开 GUI、仍可手动按 T 恢复训练；`--no-gui` 则关闭图形界面并改用命令行汇报进度，与是否训练无关。

**练习 2**：为什么说 `./instant-ngp --no-gui` 需要 `Ctrl+C` 才能停？请用 `frame()` 的返回逻辑解释。

**参考答案**：主循环是 `while (testbed.frame())`。`frame()` 只在 GUI 窗口关闭（`begin_frame()` 返回 false）时返回 false（[src/testbed.cu:L3911-L3912](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3911-L3912)）；无 GUI 时根本没有窗口，`frame()` 每次都走到末尾返回 true（[src/testbed.cu:L4033](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4033)），于是循环永不结束。

---

### 4.4 四种基元示例与模式自动识别

#### 4.4.1 概念说明

instant-ngp 的四种基元（NeRF / SDF / Image / Volume）共用同一个可执行文件，区别只在于你喂给它什么数据。程序不需要你声明「我要训 NeRF」，而是根据**文件类型自动判断**应该进入哪种模式。这个判断由一个叫 `mode_from_scene` 的函数完成，它根据路径是目录还是文件、以及文件扩展名来下结论。

四种基元各有一个官方示例（README 中给出）：

| 基元 | 模式 | 官方示例 | 输入类型 |
| :--- | :--- | :--- | :--- |
| 神经辐射场 | `Nerf` | `data/nerf/fox` | 一个目录（含 `transforms.json` + `images/`） |
| 有向距离场 | `Sdf` | `data/sdf/armadillo.obj` | 三角网格 `.obj` / `.stl` |
| 神经图像 | `Image` | `data/image/albert.exr` | 图片（`.exr` / `.png` / `.jpg` / `.bin` 等） |
| 神经体素 | `Volume` | `wdas_cloud_quarter.nvdb` | NanoVDB 体素 `.nvdb` |

#### 4.4.2 核心流程

模式的判定规则（伪代码）：

```
mode_from_scene(path):
    if path 不存在:           return None
    if path 是目录 或 .json:  return Nerf     ← NeRF 数据集是目录
    if 扩展名是 obj / stl:    return Sdf
    if 扩展名是 nvdb:         return Volume
    else:                     return Image    ← 其余一律当图片
```

注意第四条：**所有不被前面规则匹配的文件，默认当成图片**。源码注释里说"列举所有图片格式太麻烦（exr/bin/jpg/png/tga/hdr…）"，所以采用「兜底」策略。这也意味着，给一个不认识的扩展名，程序会尝试当图片加载，加载失败才会报错。

枚举本身的定义非常简洁：

[include/neural-graphics-primitives/common.h:L149-L155](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h#L149-L155) —— `enum class ETestbedMode : int { Nerf, Sdf, Image, Volume, None };`

#### 4.4.3 源码精读

`mode_from_scene` 的真实实现：

[src/common_host.cu:L144-L160](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu#L144-L160) —— 按目录/.json → Nerf、obj/stl → Sdf、nvdb → Volume、其余 → Image 的顺序判定。

那么位置参数 `./instant-ngp data/nerf/fox` 是怎么走到 `mode_from_scene` 的？路径是：位置参数 → `load_file` →（不是 .ingp/.msgpack/.json）→ `load_training_data` → 内部调用 `mode_from_scene` 设定模式。关键落点在这里：

[src/testbed.cu:L353-L368](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L353-L368) —— `load_file` 先排除快照格式（.ingp/.msgpack）。

[src/testbed.cu:L370-L401](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L370-L401) —— .json 文件按内容细分（快照 / 网络配置 / 相机路径）；**其余一切（含目录、.obj、.exr、.nvdb）都被当作训练数据，交给 `load_training_data(path)`**。

也就是说，对 `fox`（目录）和 `armadillo.obj`（.obj），`load_file` 都会落到第 401 行的 `load_training_data`，再由 `mode_from_scene` 区分出 Nerf 与 Sdf。（`load_file` 的完整分支逻辑是 u2-l3 的主题，本讲只需理解这条主干。）

四种示例在 README 里的官方命令（运行目录为仓库根，可执行文件在 `build/` 下）：

- **NeRF fox**：[README.md 的 “NeRF fox” 小节](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#nerf-fox) —— `./instant-ngp data/nerf/fox`，或直接把 `data/nerf/fox` 文件夹拖进窗口。
- **SDF armadillo**：`./instant-ngp data/sdf/armadillo.obj`。
- **Image albert**：`./instant-ngp data/image/albert.exr`。
- **Volume cloud**：需先下载 Disney 云的 `wdas_cloud_quarter.nvdb`（仓库内 `data/volume` 为空，需自行下载），再 `./instant-ngp wdas_cloud_quarter.nvdb`。

> 说明：本仓库 `data/nerf/fox`（含 `images/` 与 `transforms.json`）、`data/sdf/armadillo.obj`、`data/image/albert.exr` 都已随仓库提供，可直接运行；`data/volume` 目录为空，体素示例需要按 README 指引从 Google Drive 下载 `.nvdb` 文件。

#### 4.4.4 代码实践（本讲核心实践）

1. **实践目标**：列出所有命令行参数；并预测/验证两条命令各进入哪个模式。
2. **操作步骤**：
   - **第一步（列参数）**：通读 [src/main.cu:L36-L117](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L36-L117)，把 4.2.3 节那张参数表抄写一遍，确认无遗漏（共 12 个参数对象）。
   - **第二步（预测模式）**：对照 `mode_from_scene`（[src/common_host.cu:L144-L160](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu#L144-L160)），填写下表：

     | 命令 | 路径类型 | 扩展名 | 命中规则 | 进入模式 |
     | :--- | :--- | :--- | :--- | :--- |
     | `./instant-ngp data/nerf/fox` | 目录 | （无） | 目录 → Nerf | ? |
     | `./instant-ngp data/sdf/armadillo.obj` | 文件 | `.obj` | obj/stl → Sdf | ? |

   - **第三步（可选，需 GPU 编译版）**：在 `build/` 目录下分别运行上面两条命令，观察 GUI 标题栏或界面里显示的当前模式。
3. **需要观察的现象**：
   - `data/nerf/fox` 是目录，命中 `scene_path.is_directory()` 分支 → 模式为 **Nerf**，随后加载其中的 `transforms.json` 与图片开始训练一只狐狸的辐射场。
   - `data/sdf/armadillo.obj` 扩展名为 `.obj`，命中 obj/stl 分支 → 模式为 **Sdf**，随后加载该三角网格并用它生成距离场训练样本。
4. **预期结果（即「模式」一列的答案）**：fox → `Nerf`；armadillo.obj → `Sdf`。
5. **若无法运行**（无 GPU 或未编译）：本实践即为「源码阅读型实践」，结论已由 `mode_from_scene` 源码佐证，标注「待本地验证」实际渲染效果。

#### 4.4.5 小练习与答案

**练习 1**：如果运行 `./instant-ngp data/image/albert.exr`，会进入哪个模式？为什么？

**参考答案**：进入 `Image` 模式。`.exr` 既不是目录/json，也不是 obj/stl/nvdb，于是命中 `mode_from_scene` 的最后一条「兜底当图片」分支（[src/common_host.cu:L156-L157](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu#L156-L157)）。

**练习 2**：为什么 README 里体素示例用的文件 `wdas_cloud_quarter.nvdb` 在仓库 `data/volume` 里找不到？

**参考答案**：因为该 NanoVDB 体素文件体积较大且来自 Disney 动画数据集（CC BY-SA 3.0），仓库不内置，需要按 README 指引从 Google Drive 自行下载；下载后扩展名 `.nvdb` 会命中 `mode_from_scene` 的 Volume 分支（[src/common_host.cu:L154-L155](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu#L154-L155)）。

**练习 3**：`./instant-ngp data/nerf/fox` 用的是位置参数路径，而 README 也写「可拖入窗口」。这两者在代码里走的是同一个入口吗？

**参考答案**：是的。拖入窗口与命令行位置参数最终都调用 `testbed.load_file(path)`（命令行见 [src/main.cu:L153-L155](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L153-L155)），由 `load_file` 统一分发。

---

## 5. 综合实践

把本讲内容串起来，完成一次「从命令行到模式判定」的完整推演。

**任务**：假设你已经按 u1-l3 编译出 `build/instant-ngp`，请完成以下流程。

1. 运行 `./build/instant-ngp --help`，截取帮助文本，把每个参数与 4.2.3 节的表格一一对应，标出哪些是 `Flag`、哪些是 `ValueFlag`、哪个是位置参数。
2. 不实际运行，仅依据 `mode_from_scene`（[src/common_host.cu:L144-L160](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu#L144-L160)）填写下表：

   | 命令 | 进入模式 | 理由（命中的分支） |
   | :--- | :--- | :--- |
   | `./instant-ngp data/nerf/fox` | Nerf | 目录 |
   | `./instant-ngp data/sdf/armadillo.obj` | Sdf | obj/stl |
   | `./instant-ngp data/image/albert.exr` | Image | 兜底当图片 |
   | `./instant-ngp wdas_cloud_quarter.nvdb` | Volume | nvdb |

3. 思考题：如果你想要「训 1000 步就自动停，并保存快照」，`./instant-ngp` 本身做不到（无头模式下它不会自动停）。请翻看 `scripts/run.py`，找到对应的参数（提示：`--n_steps` 与 `--save_snapshot`，见 [scripts/run.py:L37-L71](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L37-L71)），写出等价命令，例如：

   ```sh
   python scripts/run.py data/nerf/fox --n_steps 1000 --save_snapshot fox.ingp
   ```

   这条命令复用了与本讲完全相同的 `load_file` 流程（[scripts/run.py:L97-L101](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L97-L101)），只是把帧循环包在了带步数上限的 Python 循环里（[scripts/run.py:L205-L220](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L205-L220)）。

**验收标准**：你能不看源码说出四种基元示例各自进入的模式，并解释「为什么 `--mode` 参数被废弃」。`run.py` 的深入用法会在 u7-l2 专门讲解，本讲只需建立「它是命令行超集」的印象。

## 6. 本讲小结

- instant-ngp 的 C 入口 `main()`（[src/main.cu:L199](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L199)）只做平台适配与参数收集，真正逻辑在 `main_func()`（[src/main.cu:L29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L29)）。
- 命令行用 `args.hxx` 解析，共 12 个参数对象，涵盖帮助、版本、场景、网络配置、快照、宽高、VR、`--no-gui`、`--no-train`、位置参数等；`--mode` 已废弃，模式由场景自动判定。
- 启动流程：构造 Testbed → 加载位置参数文件 / `--scene` / `--snapshot` / `--network` → 设训练开关 → 按需 `init_window` / `init_vr` → 进入 `while (testbed.frame())`。
- `--no-gui`（或用 `-DNGP_BUILD_WITH_GUI=off` 编译）进入无头模式：不开窗，改在命令行打印 `iteration` 与 `loss`；因无窗口，`frame()` 永不返回 false，程序不会自动停。
- 模式自动判定由 `mode_from_scene`（[src/common_host.cu:L144](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu#L144)）完成：目录/json→Nerf、obj/stl→Sdf、nvdb→Volume、其余→Image。
- 四种基元示例：`data/nerf/fox`（Nerf）、`data/sdf/armadillo.obj`（Sdf）、`data/image/albert.exr`（Image）、`wdas_cloud_quarter.nvdb`（Volume，需下载）。

## 7. 下一步学习建议

本讲让你能把程序「跑起来」并理解它如何选模式。接下来：

- **想理解帧循环内部在做什么**（一帧里训练与渲染如何交替、何时跳过渲染）：进入 **u2-l2「主帧循环：frame / train_and_render / train / render_frame」**，那是 `frame()` 的深入拆解。
- **想彻底搞懂文件加载分发**（`load_file` 如何区分快照/配置/相机路径/训练数据）：进入 **u2-l3「文件加载与模式自动识别」**。
- **想用 Python 自动化训练、评测、出图、导视频**：本讲已初探 `scripts/run.py`，完整能力在 **u7-l2「run.py：程序化训练、评测与渲染」**。
- 在进入 u2 之前，建议先用 `./instant-ngp data/nerf/fox` 跑通一次 GUI，获得对「秒级训练」的直观感受，再回头读源码会更轻松。
