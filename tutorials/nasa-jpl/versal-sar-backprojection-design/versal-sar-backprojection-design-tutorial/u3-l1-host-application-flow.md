# 主机应用流程与 main.cpp

## 1. 本讲目标

本讲是「主机应用」单元的第一讲。学完后你应该能够：

- 说清楚 `design/host/main.cpp` 这个运行在 ARM Cortex-A72 上的程序**接受哪些命令行参数**、每个参数代表什么。
- 画出主机程序的**五个计时阶段**（初始化 → 取数 → 生成像素 → 运行图 → 反投影）以及最后的写图收尾，并指出每个阶段调用了 `SARBackproject` 类的哪个方法、跑在哪个域（HOST / AIE）。
- 看懂 `startTime()` / `endTime()` / `printTimeDiff()` 这套基于 `CLOCK_MONOTONIC` 的计时埋点是如何工作的，为什么用它来度量性能。

本讲只讲「主机侧的编排流程与计时骨架」，**不深入** `SARBackproject` 类内部如何打开 device、如何解析 CSV、`bp()` 如何驱动 AIE——这些分别留给后续的 u3-l2、u3-l3、u3-l5。本讲对应的最小模块是：`main()` 参数解析、五阶段调用链、计时埋点。

## 2. 前置知识

阅读本讲前，你需要已经建立以下认知（来自 u1-l3 与 u2-l3）：

- **构建系统**（u1-l3）：`main.cpp` 会被交叉编译成 `sar_backproject.elf`，它链接了 AIE 编译器自动吐出的 `aie_control_xrt.cpp` 与 XRT 运行库。也就是说，主机程序能控制 AIE 图，是因为编译期把「图的句柄胶水代码」塞进了 elf。
- **ADF 图与数据驱动执行**（u2-l3）：图在运行时由宿主用 `init → run(N) → 投递数据 → wait → end` 的序列驱动；内核「输入就绪才 fire」。`main.cpp` 里的 `runGraphs()` 与 `bp()` 就是在做这种宿主侧驱动，只是细节被封装进了 `SARBackproject` 类。
- **C++ 基础**：构造函数初始化列表、静态成员函数、`struct timespec`。

如果你还记得 u1-l4 里 `common.h` 的规模宏（`PULSES`、`RC_SAMPLES`、`BC_ELEMENTS`），本讲会再次看到它们——它们决定了主机缓冲区的大小。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [design/host/main.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp) | 主机程序入口，运行在 ARM 上 | 参数解析、五阶段流程、计时埋点包裹 |
| [design/host/sar_backproject.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h) | `SARBackproject` 类声明 | 计时相关静态方法与静态变量的声明 |
| [design/host/sar_backproject.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp) | `SARBackproject` 类实现 | `startTime/endTime/printTimeDiff` 的真正实现 |
| [design/exec_scripts/run_script_hw.sh](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/exec_scripts/run_script_hw.sh) | 硬件上板的运行脚本 | 如何把命令行参数转交给 `sar_backproject.elf` |

> 提示：本讲引用的代码行号均对应 HEAD `79534466f6ae6a84894d14eff225b4f897e5d259`。

## 4. 核心概念与源码讲解

### 4.1 main() 命令行参数解析

#### 4.1.1 概念说明

`main.cpp` 编译出的 `sar_backproject.elf` 是一个**命令行程序**：它在 ARM shell 里被启动，靠 `argv` 接收 5 个位置参数来决定「加载哪个比特流、读哪些数据、结果写到哪、跑几轮」。理解参数顺序，是理解整个程序行为的第一步。

这里有一个关键的「分层」认知：用户在 shell 里敲的命令，往往**不是直接调 elf**，而是通过 `run_script_hw.sh` 间接调用。脚本会替用户「补一个参数」。这层间接关系是本模块的实践重点。

#### 4.1.2 核心流程

参数解析的伪代码：

```
接收 argc, argv
检查参数个数是否足够（不足则打印用法并退出）
把 argv[1..5] 依次绑定到 5 个语义变量：
    argv[1] -> xclbin 文件（FPGA/AIE 比特流容器）
    argv[2] -> slowtime 数据集 CSV
    argv[3] -> range compressed 数据集 CSV
    argv[4] -> 图像输出 CSV
    argv[5] -> 迭代次数 iter（整数）
把这些变量交给 SARBackproject 构造函数
```

#### 4.1.3 源码精读

入口与个数检查（注意这里有一个值得留意的细节，见下方说明）：

[design/host/main.cpp:13-17](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp#L13-L17) 检查 `argc` 并在不足时打印用法：

```cpp
int main(int argc, char ** argv) {
    if (argc < 4) {
        std::cerr << "Usage: " << argv[0] << " <xclbin file> <slowtime dataset csv file> <range compressed dataset csv file> <img out csv file> <iteration>" << std::endl;
        return -1;
    }
```

紧接着把后续位置参数绑定到语义变量：

[design/host/main.cpp:20-25](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp#L20-L25) 把 `argv` 映射为程序内变量，其中 `iter` 用 `std::stoi` 转成整数：

```cpp
    char* xclbin_filename = argv[1];
    char* st_dataset_filename = argv[2];
    char* rc_dataset_filename = argv[3];
    char* img_out_filename = argv[4];
    int iter = std::stoi(argv[5]);
```

> ⚠️ **代码阅读发现（不是要你改源码，而是训练你读码的敏锐度）**：用法串里列了 5 个必填参数（xclbin、slowtime、rc、img out、iteration），程序实际访问到了 `argv[5]`，因此**真正需要 `argc >= 6`**（程序名 + 5 个参数）。但上方的门槛写的是 `argc < 4`，阈值偏低。这意味着如果用户恰好传 4 或 5 个参数，程序能通过检查、却在执行到 `argv[4]` 或 `argv[5]` 时越界崩溃。这是一处「能编译、但在边界输入下会崩」的隐患，留作练习让你自己确认。

再看运行脚本如何转交参数：

[design/exec_scripts/run_script_hw.sh:5-15](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/exec_scripts/run_script_hw.sh#L5-L15) 脚本自己只收 4 个参数，却给 elf 传了 5 个：

```bash
if [ "$#" -ne 4 ]; then
	echo "Usage: $0 <slowtime ...> <range compressed ...> <img out csv file> <iteration>"
	exit 1
fi
st_csv_file=$1
rc_csv_file=$2
img_out_csv_file=$3
iter=$4
./sar_backproject.elf a.xclbin $st_csv_file $rc_csv_file $img_out_csv_file $iter
```

关键就在最后一行：脚本**替用户硬编码了 `a.xclbin`** 作为第一个参数，再把用户传入的 4 个参数原样顺次追加。所以 elf 端看到的 `argv[1..5]` = `a.xclbin`、slowtime、rc、img_out、iter——正好填满 main.cpp 期望的 5 个位置。这就是「脚本替用户补 xclbin」的转交机制。

#### 4.1.4 代码实践

1. **实践目标**：确认参数个数检查与实际访问之间的不一致，并跑通「脚本 → elf」的参数链。
2. **操作步骤**：
   - 通读 [main.cpp:13-25](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp#L13-L25)，数清楚程序名之外用到了 `argv[1]` 到 `argv[5]` 共 5 个槽位。
   - 在 [run_script_hw.sh](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/exec_scripts/run_script_hw.sh) 里数清楚它给 elf 传了几个参数（答案：5 个，第一个是硬编码的 `a.xclbin`）。
3. **需要观察的现象**：如果你手头有 VCK190 板卡并可执行 `./run_script_hw.sh st.csv rc.csv out.csv 1`，用 `ps` 或在脚本最后一行前加一行 `echo "$@"` 之外的调试打印，会看到 elf 收到 5 个参数。
4. **预期结果**：脚本把用户的 4 个参数「右移一位」、在首位补 `a.xclbin` 后喂给 elf。
5. 若无硬件：上述为源码阅读型实践，**待本地验证**实际运行行为。

#### 4.1.5 小练习与答案

**练习 1**：如果用户**直接**执行 `./sar_backproject.elf a.xclbin st.csv rc.csv out.csv`（漏了 iteration），会发生什么？

**参考答案**：程序名 + 4 个参数 = `argc == 5`。因为检查是 `argc < 4`，`5 < 4` 为假，所以通过检查；但随后 `std::stoi(argv[5])` 访问越界，行为未定义（通常崩溃或抛异常）。这正是上面提到的阈值偏低隐患。

**练习 2**：为什么 `run_script_hw.sh` 不让用户传 xclbin 文件名？

**参考答案**：脚本把 `a.xclbin` 硬编码，等价于「约定上板时比特流文件名固定为 `a.xclbin`」。这简化了用户调用（少敲一个参数），也意味着如果要换比特流，得改文件名或改脚本，而不是改命令行。

---

### 4.2 五阶段调用链

#### 4.2.1 概念说明

主机程序不是「一句调用就把图算完」，而是按 SAR 反投影的天然步骤，把工作拆成**有序的阶段**。每个阶段做性质完全不同的事：有的纯在 ARM 上跑（读文件、算像素坐标），有的把工作「外包」给 AIE/PL（真正的反投影计算）。

一个核心观察：`main.cpp` 用 `startTime()/endTime()/printTimeDiff()` 把**五个阶段分别计时**，但最后的 `writeImg()`（写出图像）**没有被计时包裹**。所以本讲的「五阶段」特指这五个带计时的阶段，写图是额外的收尾动作。这一点很容易被「流程图里画了六步」遮蔽，务必分清。

#### 4.2.2 核心流程

```
[阶段1 Init]        构造 SARBackproject 对象：打开 device、加载 xclbin、
                    建立 graph/PL 内核句柄、分配 DDR buffer          (HOST)
[阶段2 Fetch]       fetchRadarData()：读 slowtime/rc 两类 CSV        (HOST)
[阶段3 GenPixels]   genTargetPixels()：算目标像素 X/Y/Z 网格          (HOST)
[阶段4 RunGraphs]   runGraphs()：启动 AIE 图，让内核就绪              (HOST 发令)
[阶段5 BP]          bp()：逐脉冲把数据推进 AIE、跑反投影、结果回 DDR   (AIE)
[收尾 WriteImg]     writeImg()：把 DDR 里的图像写成 CSV              (HOST，未计时)
```

其中阶段 1–3 的数据都在主机内存里准备；阶段 4 把 AIE 图「点火」；阶段 5 才是真正把数据灌进 AIE 并拿回结果的重头戏。阶段 4 与阶段 5 的内部细节分别留给 u3-l5（`bp()` 编排）与后续讲义，本讲只看「调用顺序」。

#### 4.2.3 源码精读

**阶段 1：初始化（构造函数）**

[design/host/main.cpp:30-34](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp#L30-L34) 构造 `SARBackproject` 对象，并把这一整段计时：

```cpp
    SARBackproject::startTime();
    SARBackproject ifcc(xclbin_filename, st_dataset_filename, rc_dataset_filename, img_out_filename, iter, INSTANCES);
    SARBackproject::endTime();
    SARBackproject::printTimeDiff("Init completed (HOST)");
```

构造函数里做了什么？打开 device、`load_xclbin`、建立 `xrt::graph` 与 `dma_pkt_router` 内核句柄、按 `common.h` 宏分配 buffer。细节在 u3-l2，这里只需知道：**构造即初始化**，且它很「重」（性能文档里这步也耗时）。

**阶段 2：取数**

[design/host/main.cpp:36-43](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp#L36-L43) 调用 `fetchRadarData()` 读 CSV，失败则直接退出：

```cpp
    SARBackproject::startTime();
    if(ifcc.fetchRadarData() != 0) {
        std::cout << "\nPopulating data buffers failed (HOST)" << std::endl;
        return -1;
    }
    SARBackproject::endTime();
    SARBackproject::printTimeDiff("Populating data buffers completed (HOST)");
```

注意这一步在真实数据上**非常慢**——慢到性能文档单独点名「Populating data buffers」要花数十分鐘（因为要解析几十万行复数 CSV）。这也是为什么它被单独计时。

**阶段 3：生成目标像素**

[design/host/main.cpp:45-49](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp#L45-L49) 在 ARM 上算出要重建的像素网格坐标：

```cpp
    SARBackproject::startTime();
    ifcc.genTargetPixels();
    SARBackproject::endTime();
    SARBackproject::printTimeDiff("Generating target pixels  completed (HOST)");
```

**阶段 4：启动 AIE 图**

[design/host/main.cpp:51-56](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp#L51-L56) 把图跑起来（注意标签写的是 `(HOST)`，因为这是 ARM 侧发出的指令）：

```cpp
    // Start all AIE kernels
    std::cout << "\nRun AIE graphs (HOST)... " << std::endl;
    SARBackproject::startTime();
    ifcc.runGraphs();
    SARBackproject::endTime();
    SARBackproject::printTimeDiff("Run AIE graphs completed (HOST)");
```

**阶段 4 与 5 之间的桥接：组装 buffer 数组**

[design/host/main.cpp:58-71](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp#L58-L71) 把对象内部的 buffer 句柄「搬」进局部数组，准备喂给 `bp()`：

```cpp
    xrt::aie::bo buffers_broadcast_data_in[INSTANCES];
    xrt::aie::bo buffers_rc_in[INSTANCES];
    xrt::aie::bo buffers_xyz_px_in[INSTANCES];
    xrt::bo buffers_img_out[INSTANCES];
    int buff_num = 1;
    ...
    buffers_broadcast_data_in[0] = ifcc.m_broadcast_data_buffer;
    buffers_rc_in[0] = ifcc.m_rc_buffer;
    buffers_xyz_px_in[0] = ifcc.m_xyz_px_buffer;
    buffers_img_out[0] = ifcc.m_img_buffers[0];
```

这段揭示了一个设计意图：`bp()` 被设计成可以接收「多实例」的 buffer 数组（`INSTANCES` 与 `buff_num`），但当前 `INSTANCES = 1`（见 [main.cpp:28](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp#L28)），所以只用下标 `[0]`。源码注释也坦承「大于 1 时目前不工作」，是为未来留的接口。

**阶段 5：反投影（真正用到 AIE）**

[design/host/main.cpp:67-76](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp#L67-L76) 调用 `bp()`，计时标签是 `(AIE)`：

```cpp
    std::cout << "\nPerform Backprojection (AIE)..." << std::endl;
    ...
    SARBackproject::startTime();
    ifcc.bp(buffers_broadcast_data_in, buffers_rc_in, 
            buffers_xyz_px_in, buffers_img_out, buff_num);
    SARBackproject::endTime();
    SARBackproject::printTimeDiff("Backprojection completed (AIE)");
```

`bp()` 内部会逐脉冲通过 GMIO 把数据推进 AIE、用 RTP 控制末脉冲 dump、并启动 PL 包路由器把结果写回 DDR——这套编排是 u3-l5 的主题，本讲不展开。

**收尾：写图（未计时）**

[design/host/main.cpp:78-81](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp#L78-L81) 把结果写成 CSV，注意它**没有** `startTime/endTime` 包裹：

```cpp
    if(ifcc.writeImg() != 0) {
        std::cout << "\nWriting image failed!" << std::endl;
        return -1;
    }
```

这就是为什么本讲强调「五个计时阶段」而不是「六阶段」——写图是必要的收尾，但不在性能度量口径里。

#### 4.2.4 代码实践

1. **实践目标**：把五阶段 + 收尾整理成一张时序表。
2. **操作步骤**：对照 [main.cpp:30-81](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp#L30-L81)，按下表逐行填写。
3. **需要观察的现象**：填表时你会清楚看到「哪些阶段标 `(HOST)`、哪个标 `(AIE)`、哪个完全没计时」。
4. **预期结果**：参考下表（耗时列留空，待上板实测）。

| 阶段 | cout 提示 | 调用方法 | 主要执行域 | 计时标签 | 计时？ |
| --- | --- | --- | --- | --- | --- |
| 1 初始化 | Loading xclbin … | `SARBackproject(...)` 构造 | HOST | Init completed (HOST) | 是 |
| 2 取数 | Populating data buffers … | `ifcc.fetchRadarData()` | HOST | Populating data buffers completed (HOST) | 是 |
| 3 生成像素 | Generating target pixels … | `ifcc.genTargetPixels()` | HOST | Generating target pixels completed (HOST) | 是 |
| 4 运行图 | Run AIE graphs … | `ifcc.runGraphs()` | HOST 发令 | Run AIE graphs completed (HOST) | 是 |
| 5 反投影 | Perform Backprojection … | `ifcc.bp(...)` | AIE | Backprojection completed (AIE) | 是 |
| 收尾 写图 | （无 cout） | `ifcc.writeImg()` | HOST | — | **否** |

5. 若无硬件：表格的「耗时」列**待本地验证**，但阶段归属与计时标签可直接从源码读出。

#### 4.2.5 小练习与答案

**练习 1**：为什么「取数」这一步在真实数据上会慢到数十分鐘，而「反投影」本身只需数百毫秒？（结合 u1-l4 的规模宏）

**参考答案**：取数要把 slowtime 与 RC 两类 CSV 从文本逐行解析、把 `a+bi` 形式的复数转成 `cfloat` 写进 DDR（u3-l3 详述）；而 `PULSES×RC_SAMPLES` 的复数样本量很大，纯文本解析是 CPU 密集的串行 IO。反投影则是把这些已经驻留 DDR 的数据并行灌进上百个 AIE 内核一起算，硬件并行度极高，所以反而快得多。瓶颈在「文本进 DDR」，不在「DDR 进 AIE」。

**练习 2**：阶段 4 的标签是 `(HOST)`、阶段 5 是 `(AIE)`，但两个都是 ARM 侧发出的调用，为什么标签不同？

**参考答案**：`runGraphs()` 只是「发令让图就绪」（图进入运行态），开销很小、动作发生在主机侧，所以标 `(HOST)`；`bp()` 则会让数据真正流过 AIE 阵列并等待计算完成，**有效计算时间花在 AIE 上**，所以标 `(AIE)`。标签反映的是「时间花在哪」，不是「函数在哪发出」。

---

### 4.3 startTime / endTime / printTimeDiff 计时埋点

#### 4.3.1 概念说明

要度量「哪个阶段慢」，需要一个能跨阶段、可累加、又不受系统时间被手动改写影响的计时器。本项目选用了 POSIX 的 `clock_gettime(CLOCK_MONOTONIC, ...)`：

- **`CLOCK_MONOTONIC`** 是「单调时钟」：从一个未指定的起点开始累加，**不受管理员或 NTP 调整系统时间的影响**，专门用来测「两件事之间过了多久」。相比之下 `CLOCK_REALTIME` 可能被突然拨快/拨慢，不适合测耗时。
- **`struct timespec`** 用 `tv_sec`（秒）+ `tv_nsec`（纳秒）两段表示一个时刻，精度达到纳秒级。
- 三个静态方法构成最小计时骨架：`startTime()` 按下起点、`endTime()` 按下终点、`printTimeDiff(msg)` 计算两者差值并打印，同时累加到 `total_time`。

#### 4.3.2 核心流程

```
startTime()  -> clock_gettime 记 time_start
   ... 被测代码 ...
endTime()    -> clock_gettime 记 time_end
printTimeDiff(msg)
    -> 用 time_end - time_start 算出毫秒数
    -> 累加进 total_time
    -> printf("Elapsed time (msg): X.XX milliseconds")
```

毫秒数的换算公式（秒段差 ×1000，再加纳秒段差 ÷10⁶）：

\[
t_{\text{ms}} = (\text{tv\_sec}_{\text{end}} - \text{tv\_sec}_{\text{start}}) \times 1000 + \frac{\text{tv\_nsec}_{\text{end}} - \text{tv\_nsec}_{\text{start}}}{10^{6}}
\]

#### 4.3.3 源码精读

静态变量定义（类外初始化，所有实例共享同一份计时状态）：

[design/host/sar_backproject.cpp:15-18](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L15-L18) 定义累加器与起止时间戳：

```cpp
double SARBackproject::total_time = 0;
double SARBackproject::total_avg_time = 0;
struct timespec SARBackproject::time_start = {0, 0};
struct timespec SARBackproject::time_end = {0, 0};
```

对应的声明在头文件：

[design/host/sar_backproject.h:83-87](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h#L83-L87) 声明这些静态成员。

起止函数本身极简：

[design/host/sar_backproject.cpp:65-71](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L65-L71) 各自只记一个时间戳：

```cpp
void SARBackproject::startTime() {
    clock_gettime(CLOCK_MONOTONIC, &time_start);
}

void SARBackproject::endTime() {
    clock_gettime(CLOCK_MONOTONIC, &time_end);
}
```

差值计算与累加：

[design/host/sar_backproject.cpp:78-93](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L78-L93) 把秒与纳秒两段分别换算成毫秒再相加，并累加到 `total_time`：

```cpp
void SARBackproject::printTimeDiff(const char *msg) {
    double elapsed_time_millis;
    elapsed_time_millis = (time_end.tv_sec - time_start.tv_sec) * 1000.0;
    elapsed_time_millis += (time_end.tv_nsec - time_start.tv_nsec) / 1000000.0;
    total_time += elapsed_time_millis;
    printf("Elapsed time (%s): %.2f milliseconds\n", msg, elapsed_time_millis);
}
```

> ⚠️ **代码阅读发现**：类里还定义了 `printTotalTime()`、`printAvgTime()`、`resetTimer()`（见 [sar_backproject.cpp:73-110](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L73-L110) 与 [sar_backproject.h:61-66](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h#L61-L66)），看上去是为「多轮迭代取平均」准备的。但用搜索可以确认：**`main.cpp` 全程只调用了 `startTime/endTime/printTimeDiff` 这三个**，平均与清零函数从未被调用，`iter` 参数被存进 `m_iter` 后也没有被读。这说明当前 `main.cpp` 是「单次运行」路径，多轮平均的脚手架是预埋的、尚未接线。读源码时区分「已启用」与「预埋未用」非常重要。

#### 4.3.4 代码实践

1. **实践目标**：从运行输出里读出每个阶段的毫秒数，并验证 `total_time` 的累加语义。
2. **操作步骤**：
   - 在 [sar_backproject.cpp:78-93](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L78-L93) 处确认：每调一次 `printTimeDiff`，`total_time` 就加上本次 `elapsed_time_millis`。
   - 若有上板环境，运行 `run_script_hw.sh`，把终端里 5 行 `Elapsed time (...)` 抄下来。
3. **需要观察的现象**：终端会依次打印 5 行形如 `Elapsed time (Init completed (HOST)): XXX.XX milliseconds` 的输出；把它们手算相加，应等于「如果在 `bp()` 之后调用 `printTotalTime()` 会打印的值」。
4. **预期结果**：五段之和 ≈ 端到端（不含写图）的总耗时；其中「Populating data buffers」会显著最大。
5. 若无硬件：可在源码层面确认累加逻辑，**待本地验证**实际数值。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `CLOCK_MONOTONIC` 换成 `CLOCK_REALTIME`，在什么情况下测出来的耗时会出错？

**参考答案**：若运行期间系统时间被管理员或 NTP 调整（例如向前跳了 10 秒），`CLOCK_REALTIME` 的读数会突变，导致「终点 − 起点」算出的耗时偏离真实流逝时间（甚至出现负数或巨大正值）。`CLOCK_MONOTONIC` 不受这类调整影响，是测耗时的正确选择。

**练习 2**：`time_start` 与 `time_end` 为什么是**静态**成员、且全局只有一对，而不是每个阶段各有一对？

**参考答案**：因为 `startTime/endTime/printTimeDiff` 是「成对、串行」使用的：每段代码前调 `startTime`、后立刻调 `endTime` 再 `printTimeDiff`，下一段开始前才会再次 `startTime` 覆盖旧值。既然同一时刻只有一个正在被测的阶段，一对全局静态变量就够用，且 `printTimeDiff` 能直接引用它们而无需传参。代价是：**不能嵌套计时**（在一段里再开一段会互相覆盖），所以你不会看到本代码做嵌套度量。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿任务。

**任务**：为 `main.cpp` 写一份《主机运行时序说明书》，包含三个部分。

1. **参数链路图**：画出从用户在 shell 敲命令，到 `main()` 内 `argv[1..5]` 被绑定的完整链路。要求标注：
   - 用户传给 `run_script_hw.sh` 的 4 个参数；
   - 脚本硬编码补上的 `a.xclbin`；
   - elf 收到的 5 个位置参数及其语义变量名。
2. **五阶段时序表**：照 4.2.4 的表格，补上「输入数据」「输出数据」两列。例如阶段 2 的输入是两个 CSV 文件路径、输出是填好的 `m_broadcast_data_array` / `m_rc_array`；阶段 5 的输入是三个 GMIO buffer + 图像输出 buffer、输出是写回 DDR 的图像。
3. **计时口径说明**：用一句话回答——「如果有人问『这套程序端到端跑了多久』，你应该把哪几段时间加起来？为什么不含写图？」并指出 `total_time` 累加器在当前 `main.cpp` 里有没有真正被读出来（提示：找 `printTotalTime` 的调用点）。

**验收标准**：

- 参数链路图能清楚体现「脚本补 xclbin」这一步；
- 时序表里阶段 1–5 都带 `(HOST)` 或 `(AIE)` 标签，且写图行明确标注「未计时」；
- 计时口径说明正确指出 `printTotalTime/printAvgTime` 当前未被调用，所以「端到端总耗时」目前只能靠人工把 5 行输出相加得到。

> 本任务为源码阅读 + 文档型实践，无需运行硬件；如有上板条件，可用实测毫秒数填充时序表。

## 6. 本讲小结

- `main.cpp` 是运行在 ARM 上的命令行程序，通过 `argv[1..5]` 接收 xclbin、slowtime CSV、RC CSV、图像输出 CSV、迭代次数共 5 个参数；`run_script_hw.sh` 替用户硬编码 `a.xclbin` 并转发其余 4 个参数。
- 程序把 SAR 反投影拆成五个**计时阶段**：初始化（构造）→ 取数（`fetchRadarData`）→ 生成像素（`genTargetPixels`）→ 运行图（`runGraphs`）→ 反投影（`bp`），外加一个**未计时**的写图收尾（`writeImg`）。
- 前四阶段标签是 `(HOST)`（主机侧准备与发令），第五阶段标签是 `(AIE)`（真正并行计算发生在 AIE 阵列）；标签反映「时间花在哪」。
- 计时基于 `clock_gettime(CLOCK_MONOTONIC, ...)` 与 `struct timespec`，由 `startTime/endTime` 记起止、`printTimeDiff` 算毫秒差并累加进 `total_time`。
- 读码时注意到两处「预埋但未启用」：`argc < 4` 阈值偏低（实际需要 `argc >= 6`）；`printTotalTime/printAvgTime/resetTimer` 与 `m_iter` 已定义但当前未被调用——说明现在走的是单次运行路径。
- `bp()` 被设计成可接收多实例 buffer 数组（`INSTANCES`、`buff_num`），但当前固定为 1，只用下标 `[0]`。

## 7. 下一步学习建议

本讲只看了「流程骨架」，每一阶段的内部实现都值得深入：

- 想知道构造函数如何打开 device、加载 xclbin、建立 graph/kernel 句柄、按 `common.h` 宏分配 buffer → 继续读 **u3-l2（SARBackproject 类与 XRT 初始化）**。
- 想知道 `fetchRadarData()` 如何用正则解析 `a+bi` 复数 CSV → 读 **u3-l3（从 CSV 读取雷达数据）**。
- 想知道 `genTargetPixels()` 与 `unwrap()` 如何由方位角生成像素网格 → 读 **u3-l4（目标像素生成与方位角解卷绕）**。
- 想知道 `bp()` 如何逐脉冲把数据推进 AIE、用 RTP 控制 dump、并配合 PL 包路由器 → 读 **u3-l5（用 XRT 编排 AIE 图与 PL 内核）**。

如果你更想先看「被驱动的那一侧」，也可以跳到 **第 4 单元（AIE 图拓扑）**，看 `runGraphs/bp` 背后那张图是如何搭起来的，再回头读 u3-l5。
