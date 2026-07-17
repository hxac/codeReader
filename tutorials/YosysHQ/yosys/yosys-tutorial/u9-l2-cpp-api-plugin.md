# C++ API：把 Yosys 作为库嵌入

## 1. 本讲目标

上一讲（u9-l1）你学会了「写一个 Pass，编译成 `.so` 插件，再用 `yosys -m` 加载」。在那个模式里，**yosys 是宿主，你的代码是客人**——你只能扩展 yosys 的命令集，不能决定程序何时启动、何时退出。

本讲反过来：**让你的程序当宿主，把 yosys 当成一个普通的 C++ 库（`libyosys`）链接进来**。你在自己的 `main()` 里调用几个库函数，就能完整地驱动一次「读 HDL → 综合 → 写网表」。

学完后你应当掌握：

1. 区分「插件嵌入」与「共享库嵌入」两种方式，知道各自适用场景。
2. 用 `yosys_setup()` / `run_pass()` / `yosys_shutdown()` 在独立 C++ 程序中初始化并驱动 yosys。
3. 理解 `run_pass` / `run_frontend` / `run_backend` / `shell` 这组「驱动接口」与命令行 `yosys` 程序的等价关系。
4. 知道如何构建并链接 `libyosys`，并了解 `scopeinfo` / `ConstEval` 等高级 API 的入口。

## 2. 前置知识

阅读本讲前，请确保已理解以下概念（在前序讲义中建立）：

- **Pass 与注册机制**（u4-l1、u9-l1）：所有命令都是 `Pass`，经全局表 `pass_register` 分发，`Pass::call(design, command)` 是命令执行的总入口。
- **RTLIL Design**（u2-l2）：综合的全部状态都挂在全局对象 `yosys_design`（一个 `RTLIL::Design*`）上。
- **driver 调度**（u4-l4、u1-l2）：命令行 `yosys` 程序的 `main()` 按「读前端 → 跑脚本/命令 → 进 shell 或写后端」的顺序编排。
- **命名空间**：Yosys 几乎所有公开符号都位于 `namespace Yosys`（即宏 `YOSYS_NAMESPACE`）中。

一个关键直觉：**命令行 `yosys` 程序本身，就是 `libyosys` 的一个消费者**。它做的事，你在自己的程序里用同样的库函数也能做。本讲会反复印证这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [examples/cxx-api/demomain.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cxx-api/demomain.cc) | 「把 yosys 当库用」的最小完整示例：自带 `main()`，链接 `libyosys`，跑一次综合。 |
| [examples/cxx-api/evaldemo.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cxx-api/evaldemo.cc) | 一个插件示例，演示 `ConstEval` 求值 API（**插件**形态，对照用）。 |
| [examples/cxx-api/scopeinfo_example.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cxx-api/scopeinfo_example.cc) | 一个插件示例，演示 `scopeinfo` 源码层级索引 API（高级用法）。 |
| [kernel/yosys.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.h) | 库的「门面头文件」，声明 `yosys_setup`/`run_pass` 等驱动接口。 |
| [kernel/yosys.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc) | 上述接口的实现。 |
| [kernel/yosys_common.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys_common.h) | 定义 `YOSYS_NAMESPACE`（=`Yosys`）等基础宏。 |
| [kernel/driver.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc) | 命令行 `yosys` 的 `main()`，证明 CLI 与库共用同一套驱动接口。 |
| [CMakeLists.txt](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt) | 定义 `libyosys` 构建目标与安装选项。 |

> 说明：`examples/cxx-api/` 目录下既有「库」示例（`demomain.cc`，自带 `main()`），也有「插件」示例（`evaldemo.cc`、`scopeinfo_example.cc`，是 `Pass` 子类、无 `main()`）。本讲聚焦前者，并用后者引出高级 API。

## 4. 核心概念与源码讲解

### 4.1 两种嵌入方式：插件 `.so` 与共享库 `libyosys`

#### 4.1.1 概念说明

把 yosys 的能力接入你自己的代码，有两条路：

1. **插件方式（u9-l1）**：你写一个 `Pass` 子类，用 `yosys-config --build mypass.so mypass.cc` 编译成动态库，再 `yosys -m mypass.so` 加载。
   - 谁是宿主？**yosys 可执行程序**。
   - 你的代码角色：给 yosys 增加一条新命令。
   - 生命周期：yosys 决定何时 `yosys_setup()`、何时 `yosys_shutdown()`，你只实现 `execute()`。

2. **共享库方式（本讲）**：你写一个普通 C++ 程序（自带 `main()`），把 `libyosys` 当成普通依赖链接进来，在 `main()` 里主动调用 `yosys_setup()` / `run_pass()` / `yosys_shutdown()`。
   - 谁是宿主？**你的程序**。
   - 你的代码角色：驱动者，决定何时综合、综合什么、结果去哪。
   - 生命周期：由你的 `main()` 全权掌控。

#### 4.1.2 核心流程

两种方式的差异，本质在于「谁拥有 `main()`」与「谁调用生命周期函数」：

```
插件方式：   yosys main() ──> yosys_setup() ──> load_plugin(你的.so) ──> 你的 execute() ──> yosys_shutdown()
共享库方式： 你的 main() ──> yosys_setup() ──> run_pass(...) 多次 ──> yosys_shutdown()
```

选型建议：

| 维度 | 插件 `.so` | 共享库 `libyosys` |
|------|-----------|-------------------|
| 谁有 `main()` | yosys | 你的程序 |
| 何时用 | 给 yosys 加命令、做综合脚本内的局部变换 | 把综合嵌入更大系统（CI、Web 服务、GUI、批处理） |
| 编译产物 | `mypass.so`（动态库，无 `main`） | 你的可执行程序 |
| 调试 | 要 `-m` 加载 | 直接运行自己的程序 |
| 复杂度 | 低（只写一个 Pass） | 中（要管生命周期、日志、链接） |

#### 4.1.3 源码精读

`demomain.cc` 拥有自己的 `main()`，是「共享库方式」的标志：

```cpp
int main()
{
    Yosys::yosys_setup();
    Yosys::yosys_banner();
    Yosys::run_pass("read_verilog example.v");
    Yosys::run_pass("synth -noabc");
    Yosys::yosys_shutdown();
    return 0;
}
```

注意每个调用都带 `Yosys::` 前缀。这个命名空间由宏定义：

```cpp
// kernel/yosys_common.h
#define YOSYS_NAMESPACE          Yosys
#define YOSYS_NAMESPACE_BEGIN    namespace Yosys {
#define USING_YOSYS_NAMESPACE    using namespace Yosys;
```

参见 [kernel/yosys_common.h:104-110](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys_common.h#L104-L110)——`demomain.cc` 用显式 `Yosys::` 前缀，而 `evaldemo.cc` / `scopeinfo_example.cc` 用 `USING_YOSYS_NAMESPACE` 把整个命名空间引入，两种写法等价，按你的代码风格二选一。

对比 `evaldemo.cc`：它**没有** `main()`，而是一个 `Pass` 子类，注释里写明了它是用 `yosys -m evaldemo.so` 加载的插件：

```cpp
struct EvalDemoPass : public Pass
{
    EvalDemoPass() : Pass("evaldemo") { }
    void execute(vector<string>, Design *design) override { ... }
} EvalDemoPass;
```

参见 [examples/cxx-api/evaldemo.cc:21-23](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cxx-api/evaldemo.cc#L21-L23)。这就是 u9-l1 讲过的插件骨架，不在本讲重点，但放在同一目录里正可作对照。

#### 4.1.4 代码实践

**实践目标**：用肉眼区分「库示例」与「插件示例」。

**操作步骤**：

1. 打开 `examples/cxx-api/` 下三个 `.cc` 文件。
2. 各自查找：是否有 `int main()`？是否继承 `Pass`？文件顶部注释里的构建命令是 `yosys-config --build xxx.so` 还是 `-o demomain ... -lyosys`？

**需要观察的现象**：

- `demomain.cc`：有 `main()`，不继承 `Pass`，构建命令含 `-lyosys` → **库方式**。
- `evaldemo.cc` / `scopeinfo_example.cc`：无 `main()`，继承 `Pass`，构建命令为 `--build xxx.so` → **插件方式**。

**预期结果**：能准确说出三者各属哪一类，并解释「`-lyosys` 表示把库链进可执行程序」与「`.so` 表示被 yosys 用 `dlopen` 加载」的区别。

#### 4.1.5 小练习与答案

**练习 1**：如果我想在一个 Qt GUI 程序里点按钮触发一次综合，该选哪种方式？为什么？
**答案**：共享库方式。GUI 程序自己有 `main()` 和事件循环，需要主动掌控 `yosys_setup`/`run_pass`/`yosys_shutdown` 的时机；插件方式要求以 `yosys` 可执行程序为宿主，不适合嵌入到已有 GUI 进程。

**练习 2**：`Yosys::run_pass(...)` 与 `USING_YOSYS_NAMESPACE; run_pass(...)` 有何异同？
**答案**：完全等价，都调用命名空间 `Yosys` 中的 `run_pass`。前者显式限定，适合避免命名冲突；后者一次性引入整个命名空间，代码更简洁，但可能引入名字污染。

---

### 4.2 demomain 入口：yosys 的生命周期

#### 4.2.1 概念说明

把 yosys 当库用，核心是三个动作构成的**生命周期**：

- `yosys_setup()`：一次性初始化（注册所有 Pass、创建全局 `yosys_design`）。
- 中间：任意次 `run_pass()` / `run_frontend()` / `run_backend()` 驱动综合。
- `yosys_shutdown()`：一次性清理（注销 Pass、销毁 design、关闭文件）。

这三个函数都必须配对调用，且 `setup` 必须早于任何 `run_*`，`shutdown` 必须晚于所有 `run_*`。

#### 4.2.2 核心流程

`demomain.cc` 的完整生命周期如下：

```
log_streams.push_back(&cout)   # （可选）把 yosys 日志导向标准输出
log_error_stderr = true        # （可选）让 log_error 同时打到 stderr
yosys_setup()                  # 初始化：注册 Pass、new Design、log_push
yosys_banner()                 # （可选）打印版本横幅
run_pass("read_verilog …")     # 综合：可重复任意次
run_pass("synth -noabc")
run_pass("clean -purge")
run_pass("write_blif …")
yosys_shutdown()               # 清理：done_register、delete Design、log_pop
```

`setup` 与 `shutdown` 都是**幂等**的——内部用两个布尔量 `already_setup` / `already_shutdown` 做重入保护，重复调用不会出错。这对「库被多次初始化」或「宿主进程里 yosys 被反复进出」的场景很重要。

#### 4.2.3 源码精读

demomain 在 `setup` 之前先做了两件可选的日志配置（[examples/cxx-api/demomain.cc:8-12](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cxx-api/demomain.cc#L8-L12)）：

```cpp
Yosys::log_streams.push_back(&std::cout);   // 让 yosys 的 log() 也输出到 stdout
Yosys::log_error_stderr = true;             // 致命错误同时进 stderr
Yosys::yosys_setup();
Yosys::yosys_banner();
```

`log_streams` 是一个 `std::vector<std::ostream*>`，`log()` 族函数会把文本广播到这里的每一个流（[kernel/log.h:89](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.h#L89)）。默认它不含 `std::cout`，所以 demomain 主动加进去，才能在终端看到综合过程。

`yosys_setup()` 的实现（[kernel/yosys.cc:236-268](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L236-L268)）做了四件关键事：

```cpp
void yosys_setup()
{
    if(already_setup) return;          // 幂等保护
    already_setup = true;
    already_shutdown = false;

    IdString::ensure_prepopulated();   // 1. 预填知名 IdString（见 u3-l3）
    init_share_dirname();              // 2. 定位 share 目录（techlibs 资源）
    init_abc_executable_name();        //    定位 yosys-abc 可执行程序
    Pass::init_register();             // 3. 把所有静态 Pass 搬进 pass_register（见 u4-l1）
    yosys_design = new RTLIL::Design;  // 4. 创建全局设计对象
    yosys_celltypes.static_cell_types = StaticCellTypes::categories.is_known;
    log_push();                        //    压入日志标题栈
}
```

`Pass::init_register()` 正是 u4-l1 讲过的去中心化注册：遍历 `first_queued_pass` 链表，把每条命令登记进三张表。没有这一步，`run_pass` 会找不到任何命令。

`yosys_shutdown()` 对称清理（[kernel/yosys.cc:275-319](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L275-L319)）：

```cpp
void yosys_shutdown()
{
    if(already_shutdown) return;       // 幂等保护
    already_setup = false;
    already_shutdown = true;
    log_pop();                         // 弹出日志标题栈
    Pass::done_register();             // 注销所有 Pass
    delete yosys_design;               // 销毁全局设计对象
    yosys_design = NULL;
    RTLIL::OwningIdString::collect_garbage();   // 回收 IdString
    // 关闭日志文件、终结 TCL/Python、dlclose 插件……
}
```

注意 `delete yosys_design` 之后，`yosys_design` 被置空——所以**绝对不能**在 `shutdown` 之后再访问 design 或调用 `run_pass`。

#### 4.2.4 代码实践

**实践目标**：理解日志配置对可见性的影响。

**操作步骤**：

1. 阅读上面 `yosys_setup` 的源码，确认它「创建 design、注册 pass」两件事。
2. （可选，待本地验证）把 `demomain.cc` 里的 `Yosys::log_streams.push_back(&std::cout);` 这一行**注释掉**，重新编译运行（构建方法见 4.4），对比终端输出差异。

**需要观察的现象**：

- 保留该行时，`read_verilog`、`synth` 各阶段的 `-- Running command … --` 提示会打到 stdout。
- 注释掉后，这些日志可能不再出现在 stdout（取决于默认流配置）。

**预期结果**：能用一句话解释「为什么 demomain 要手动 `push_back(&std::cout)`」——因为 `log_streams` 默认不含标准输出，库使用者需自行决定日志去向。

#### 4.2.5 小练习与答案

**练习 1**：如果程序里调了两次 `yosys_setup()` 会怎样？
**答案**：第二次是空操作。`already_setup` 标志使函数在入口立即 `return`，不会重复注册 Pass 或重复 `new Design`。

**练习 2**：为什么 `yosys_shutdown()` 里要 `delete yosys_design` 并置空？
**答案**：释放设计占用的全部 RTLIL 内存（模块、线网、单元……），避免泄漏；置空是为了让后续访问「立即暴露问题」而非读到悬空指针。

---

### 4.3 run_pass / run_frontend / run_backend：驱动接口

#### 4.3.1 概念说明

生命周期函数搭好舞台后，真正「干活」的是一组驱动接口，它们全部声明在 [kernel/yosys.h:74-77](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.h#L74-L77)：

```cpp
void run_pass(std::string command, RTLIL::Design *design = nullptr);
bool run_frontend(std::string filename, std::string command, RTLIL::Design *design = nullptr, std::string *from_to_label = nullptr);
void run_backend(std::string filename, std::string command, RTLIL::Design *design = nullptr);
void shell(RTLIL::Design *design);
```

它们的共同点：第二个 `design` 参数都默认为 `nullptr`，表示「用全局 `yosys_design`」。所以最简单的调用就是 `run_pass("synth")`，无需手动传 design。

最重要的直觉：**这四个函数正是命令行 `yosys` 程序内部用的同一组函数**。你用库能做的事，和命令行完全等价。

#### 4.3.2 核心流程

四个函数的分工与命令行选项一一对应：

| 库函数 | 作用 | 对应命令行场景 |
|--------|------|----------------|
| `run_pass(cmd)` | 执行任意一条命令（最通用） | `-p "cmd"` 或脚本里的一行 |
| `run_frontend(file, cmd)` | 读入一个文件（前端） | 位置参数里的输入文件、`-f` |
| `run_backend(file, cmd)` | 写出一个文件（后端） | `-b backend -o file` |
| `shell(design)` | 进入交互式 shell | 不带 `-p`/脚本时进交互 |

它们对 `command="auto"` 的处理：`run_frontend`/`run_backend` 会按文件扩展名**自动猜测**前端/后端种类（`.v`→verilog、`.il`→rtlil、`.json`→json、`.blif`→blif 等），所以你常常只需给文件名。

#### 4.3.3 源码精读

`run_pass` 极其简单——它就是把字符串命令交给 `Pass::call`（[kernel/yosys.cc:857-865](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L857-L865)）：

```cpp
void run_pass(std::string command, RTLIL::Design *design)
{
    if (design == nullptr)
        design = yosys_design;          // 默认操作全局 design
    log("\n-- Running command `%s' --\n", command);
    Pass::call(design, command);        // 复用 u4-l1 的总入口：切词 → 查表 → execute
}
```

也就是说，`run_pass("synth -noabc")` 与你在 `yosys>` 提示符下敲 `synth -noabc` 走的是**同一条代码路径**——都是 `Pass::call`。

`run_backend` 的亮点是 `auto` 猜测逻辑（[kernel/yosys.cc:867-907](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L867-L907)），节选：

```cpp
if (command == "auto") {
    if (... 以 ".blif" 结尾)  command = "blif";
    else if (... 以 ".json" 结尾) command = "json";
    else if (... 以 ".v" 结尾)   command = "verilog";
    else if (... 以 ".il" 结尾)  command = "rtlil";
    ...
}
Backend::backend_call(design, NULL, filename, command);
```

`run_frontend` 的 `auto` 逻辑对称（[kernel/yosys.cc:723-763](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L723-L763)），但它多一个特殊能力：当 `command` 被识别为 `"script"` 时，它会逐行读取 `.ys` 脚本并对每行调 `Pass::call`，还支持 `from:to` 标签区间执行。

**最有力的证据：CLI 的 `main()` 用的就是这组函数。** 看 `kernel/driver.cc` 的命令行主流程（[kernel/driver.cc:449-546](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L449-L546)）：

```cpp
yosys_setup();                                      // L449：与你库代码一样的初始化
...
for (... frontend_files ...)
    if (run_frontend((*it).c_str(), frontend_command))   // L477：读输入
        run_shell = false;
...
for (... passes_commands ...)
    run_pass(*it);                                  // L533：执行 -p 命令
...
if (run_shell)
    shell(yosys_design);                            // L544：进交互
else
    run_backend(output_filename, backend_command);  // L546：写输出
```

逐行比对：命令行 `yosys` 在 `main()` 里调的 `yosys_setup` / `run_frontend` / `run_pass` / `shell` / `run_backend`，和你在 `demomain.cc` 里调的**是同一组函数**。这从源码层面证明：把 yosys 当库用，并不损失任何能力。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：建立「库接口 = CLI 接口」的信心。

**操作步骤**：

1. 打开 [kernel/driver.cc:476-546](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L476-L546)。
2. 把这段 CLI 主循环，与 [examples/cxx-api/demomain.cc:11-19](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cxx-api/demomain.cc#L11-L19) 逐行对应。
3. 回答：`yosys -p "synth" counter.v -o out.blif` 这条命令行，对应 demomain 里哪几行？

**需要观察的现象**：CLI 的 `run_frontend`（读 `counter.v`）、`run_pass("synth")`、`run_backend("out.blif", ...)` 三步，在 demomain 里就是 `run_pass("read_verilog …")` + `run_pass("synth …")` + `run_pass("write_blif …")`（demomain 用 `run_pass` 统一表达，也可拆成 `run_frontend`/`run_backend`）。

**预期结果**：能画出 CLI 与库两种调用方式的等价对应表，确认「能力等价」。

#### 4.3.5 小练习与答案

**练习 1**：`run_pass("read_verilog top.v")` 与 `run_frontend("top.v", "auto")` 都能读入 Verilog，区别在哪？
**答案**：前者把整行当作命令交给 `Pass::call`，相当于在 shell 里敲一行；后者走专门的 `run_frontend` 路径，`"auto"` 会按扩展名猜测前端种类，并额外处理 `"script"`/`"tcl"` 等情形。功能上可互通，但 `run_frontend` 对「读文件」语义更明确。

**练习 2**：`run_pass` 的第二个参数 `design` 何时需要显式传？
**答案**：几乎不需要。默认 `nullptr` 表示用全局 `yosys_design`（`yosys_setup` 创建的那个）。只有在高级场景下，当你手动 `new` 了一个临时 `RTLIL::Design` 想在上面跑命令时，才显式传入——但这不常见。

---

### 4.4 链接 libyosys 与高级用法

#### 4.4.1 概念说明

要用库方式，你必须先有一个可链接的 `libyosys`。它由顶层 `CMakeLists.txt` 定义为一个库目标（共享或静态），并且因为默认不安装，需要显式开启安装或先构建该目标。

链接完成后，除了 `run_pass` 跑内置命令，你还能直接访问 RTLIL 对象（经 `yosys_get_design()`），并使用 `ConstEval`、`scopeinfo` 等高级 API——这些 API 在 `evaldemo.cc`、`scopeinfo_example.cc` 里以**插件**形态示范，但同样可在库代码中调用。

#### 4.4.2 核心流程

构建与链接的整体链路：

```
CMake 构建 yosys
   ├── 产出可执行 yosys（driver.cc 的 main）
   └── 产出库目标 libyosys（SHARED 或 STATIC，按 BUILD_SHARED_LIBS）
         └── 安装与否取决于 YOSYS_INSTALL_LIBRARY

编译你的程序：
   yosys-config --exec --cxx -o demomain --cxxflags --ldflags demomain.cc -lyosys
          └── --cxxflags 给出 include 路径与宏
          └── --ldflags  / -lyosys 给出库路径与链接 libyosys
```

#### 4.4.3 源码精读

`libyosys` 目标的定义在顶层 CMake（[CMakeLists.txt:445-455](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L445-L455)）：

```cmake
if (BUILD_SHARED_LIBS)
    set(libyosys_type SHARED)        # 默认：动态库 libyosys.so / libyosys.dylib
else()
    set(libyosys_type STATIC)
endif()
yosys_cxx_library(libyosys ${libyosys_type}
    OUTPUT_NAME libyosys
    INSTALL_IF ${YOSYS_INSTALL_LIBRARY}   # 默认 OFF，故默认不安装
)
yosys_link_components(libyosys PRIVATE ${library_components})
add_library(Yosys::libyosys ALIAS libyosys)
```

两个关键开关（[CMakeLists.txt:46](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L46) 与 [CMakeLists.txt:69](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L69)）：

```cmake
option(BUILD_SHARED_LIBS "Build libyosys as a shared library" ON)
option(YOSYS_INSTALL_LIBRARY "Install libyosys library" OFF)
```

也就是说，`libyosys` 默认构建为**共享库**，但默认**不安装**到系统。`demomain.cc` 顶部注释正反映了这一点——要么先 `cmake -DYOSYS_INSTALL_LIBRARY=1` 安装，要么先构建 `libyosys` 目标再用 `yosys-config` 链接：

```cpp
// Note: Use `cmake -DYOSYS_INSTALL_LIBRARY=1` or build the `libyosys` target first
// yosys-config --exec --cxx -o demomain --cxxflags --ldflags demomain.cc -lyosys
```

参见 [examples/cxx-api/demomain.cc:1-2](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cxx-api/demomain.cc#L1-L2)。`yosys-config` 是随 yosys 安装的帮助脚本，`--cxxflags` 展开成 include 目录与编译宏，`--ldflags`/`-lyosys` 展开成链接选项（参见 [misc/yosys-config.in](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/misc/yosys-config.in) 的 usage 说明）。

> 若不安装、只在构建树内用，也可在 CMake 工程里 `find_package` / 直接 `target_link_libraries(your_app PRIVATE Yosys::libyosys)`（即上面建立的 ALIAS 目标）。具体集成方式「待本地验证」。

**高级用法示范**——`scopeinfo_example.cc` 展示了源码层级（SystemVerilog generate 作用域）的索引 API。它虽是插件形态，但所用的 `ModuleHdlnameIndex` 等类同样可在库代码中访问。核心片段（[examples/cxx-api/scopeinfo_example.cc:62-83](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cxx-api/scopeinfo_example.cc#L62-L83)）：

```cpp
for (auto module : design->selected_modules()) {
    ModuleHdlnameIndex index(module);
    index.index_scopeinfo_cells();                 // 扫描 $scopeinfo 单元建立索引
    for (auto wire : module->selected_wires()) {
        auto wire_scope = index.containing_scope(wire);  // 查每根线所在的层级
        for (auto src : index.sources(wire))             // 列出其源码位置
            log(" - %s\n", src);
    }
}
```

`evaldemo.cc` 则示范了 `ConstEval`——对一个组合/时序模块，给定部分输入求输出（[examples/cxx-api/evaldemo.cc:41-51](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cxx-api/evaldemo.cc#L41-L51)）：

```cpp
ConstEval ce(module);
for (int v = 0; v < 4; v++) {
    ce.push();
    ce.set(wire_a, Const(v, GetSize(wire_a)));   // 给输入 A 赋值
    SigSpec sig_y = wire_y, sig_undef;
    if (ce.eval(sig_y, sig_undef))               // 尝试求 Y
        log("Eval results for A=%d: Y=%s\n", v, log_signal(sig_y));
    ce.pop();
}
```

这两个示例的共同点：它们都先经 `run_pass`/`read_verilog` 把设计载入 `yosys_design`，再用 C++ API 直接探查 RTLIL——这正是「库方式」的真正威力所在：**不只是跑命令，还能在 C++ 层面读写设计对象**。

#### 4.4.4 代码实践

**实践目标**：在构建树中产出可链接的 `libyosys`，并理解链接命令。

**操作步骤**（待本地验证——需先装好 yosys 的编译依赖）：

1. 构建 yosys 并显式产出/安装 libyosys：
   ```bash
   cmake -B build -DYOSYS_INSTALL_LIBRARY=1 .
   cmake --build build -j
   ```
2. 查看 `yosys-config` 给出的编译/链接选项：
   ```bash
   ./build/yosys-config --cxxflags
   ./build/yosys-config --ldflags
   ```
3. 编译 demomain（需自备一个 `example.v`，因为仓库未附带）：
   ```bash
   # 先写一个最小 example.v：
   #   module example(input a, b, output y); assign y = a & b; endmodule
   ./build/yosys-config --exec --cxx -o demomain --cxxflags --ldflags \
       examples/cxx-api/demomain.cc -lyosys
   ./demomain
   ```

**需要观察的现象**：

- 步骤 2 输出应包含指向 `kernel/` 的 `-I` include 路径，以及 `-lyosys` 等链接项。
- 步骤 3 运行后，应看到 banner、`read_verilog`/`synth`/`clean`/`write_blif` 各阶段日志，并在当前目录生成 `example.blif`。

**预期结果**：能解释 `-lyosys` 的作用（链接 libyosys 共享库），并确认 demomain 与命令行 `yosys` 综合出的 `example.blif` 内容一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么默认 `YOSYS_INSTALL_LIBRARY=OFF`？
**答案**：大多数用户只装 `yosys` 可执行程序即可，不需要把开发头文件和库装到系统。只有二次开发者（要把 yosys 嵌入自己程序）才需要显式开启，避免污染普通用户的系统。

**练习 2**：`scopeinfo_example` 与 `evaldemo` 都是插件（`.so`），但本讲说它们示范的 API「也可在库代码中用」，依据是什么？
**答案**：它们访问的 `design->selected_modules()`、`ModuleHdlnameIndex`、`ConstEval` 等都是 `libyosys` 暴露的公开 C++ 接口。插件与库代码的区别仅在「谁提供 `main()` 和生命周期」，一旦 `yosys_setup()` 跑完、`yosys_design` 就绪，两者面对的 API 表面完全相同。

---

## 5. 综合实践

**任务**：参照 `demomain.cc`，写一个最小 C++ 程序，把一个 Verilog 文件读入、综合、再用 `write_verilog` 输出综合后网表，最后干净退出。要求：

1. 自带 `main()`，链接 `libyosys`（不是写成插件）。
2. 调用 `yosys_setup()` / `yosys_banner()` / 若干 `run_pass` / `yosys_shutdown()`。
3. 把日志导向 `std::cout`。

**参考实现（示例代码，需自备 `top.v`，行为待本地验证）**：

```cpp
// mini_synth.cc —— 示例代码（非仓库原有文件）
#include <kernel/yosys.h>

int main()
{
    Yosys::log_streams.push_back(&std::cout);   // 日志进标准输出
    Yosys::log_error_stderr = true;

    Yosys::yosys_setup();                       // 1. 初始化
    Yosys::yosys_banner();

    Yosys::run_pass("read_verilog top.v");      // 2. 读设计
    Yosys::run_pass("synth -noabc");            // 3. 综合（不用 abc，免外部依赖）
    Yosys::run_pass("clean -purge");            //    清理悬空线/死单元
    Yosys::run_pass("write_verilog synth_out.v"); // 4. 写网表

    Yosys::yosys_shutdown();                    // 5. 清理
    return 0;
}
```

**自测步骤**：

1. 准备 `top.v`，例如：
   ```verilog
   module top(input clk, input [3:0] a, output reg [3:0] q);
     always @(posedge clk) q <= a + 1;
   endmodule
   ```
2. 用 `yosys-config` 编译链接（命令见 4.4.4，把 `demomain.cc` 换成 `mini_synth.cc`）。
3. 运行 `./mini_synth`，检查是否生成 `synth_out.v`。
4. 用 `diff` 对比：你的 `mini_synth` 输出的 `synth_out.v`，与 `yosys -p "read_verilog top.v; synth -noabc; clean -purge; write_verilog ref_out.v"` 的输出在结构上是否一致（单元数量、类型应相同）。

**验收标准**：

- 程序能完整跑完 setup→read→synth→write→shutdown 不崩溃。
- 生成的 `synth_out.v` 含 `$dff`、`$adff` 或展开后的门级单元（取决于 `synth` 阶段）。
- 你能用一句话说明：你的 `main()` 与 `kernel/driver.cc` 的 `main()` 调用的是同一组驱动函数。

## 6. 本讲小结

- 把 yosys 接入自己代码有两条路：**插件 `.so`**（yosys 当宿主，你加命令，u9-l1）与**共享库 `libyosys`**（你的程序当宿主，本讲）。
- 库方式的生命周期是 `yosys_setup()` → 若干 `run_pass`/`run_frontend`/`run_backend` → `yosys_shutdown()`；`setup`/`shutdown` 均幂等。
- 驱动接口 `run_pass`/`run_frontend`/`run_backend`/`shell` 全部默认操作全局 `yosys_design`，且**与命令行 `yosys` 的 `main()` 用的是同一组函数**——能力完全等价。
- `run_pass(cmd)` 本质是把字符串交给 `Pass::call`，与在 shell 里敲命令走同一条路径；`run_frontend`/`run_backend` 的 `"auto"` 会按扩展名猜前后端种类。
- `libyosys` 默认构建为共享库但默认不安装；用 `yosys-config --cxxflags --ldflags ... -lyosys` 或 CMake 的 `Yosys::libyosys` ALIAS 目标链接。
- 库方式不止能跑命令，还能经 `yosys_get_design()` 直接访问 RTLIL，使用 `ConstEval`、`scopeinfo` 等高级 API（`evaldemo.cc`、`scopeinfo_example.cc` 示范）。

## 7. 下一步学习建议

- **学 Python 绑定（u9-l3）**：如果你更希望在脚本环境里驱动综合，下一讲讲解基于 pybind11 的 `pyosys`，它把同一套 RTLIL/Pass 模型暴露给 Python，思路与本讲的 C++ 库接口一一对应。
- **深入 RTLIL 对象访问**：想用库方式做设计分析（而非只跑命令），复习 u2-l2/u3-l1 的 `Design`/`Module`/`Cell` 接口，并尝试在 `run_pass` 之后用 `yosys_get_design()->top_module()->cells()` 遍历单元。
- **阅读相关源码**：`kernel/yosys.cc` 的 `run_frontend`（L723）/`run_pass`（L857）/`run_backend`（L867）三个函数体都很短，逐行读完能彻底打通「库接口 = CLI 接口」的认知。
- **高级 API 探索**：对照 `kernel/consteval.h` 与 `kernel/scopeinfo.h` 的头文件，把 `evaldemo`/`scopeinfo_example` 的 C++ 调用与声明一一对应。
