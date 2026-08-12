# 从 CSV 读取雷达数据

## 1. 本讲目标

本讲聚焦主机应用五阶段中的「取数」阶段，逐行精读 `SARBackproject::fetchRadarData()`。读完本讲你应当能够：

- 说清 **slowtime（慢时间）** 与 **range compressed（距离压缩，RC）** 两类 CSV 的格式差异，以及它们各自如何被解析。
- 拆解 `a+bi` 形式复数所用到的 **正则表达式**，解释每个分组捕获了什么、为什么虚部符号是必选的。
- 理解解析后的数据如何按行优先（row-major）写入 `.map<>()` 映射回来的 device buffer 数组（`m_broadcast_data_array` / `m_rc_array`），并知道这些 buffer 后续由谁消费。
- 估算 RC 数据在 DDR 中实际占用的字节数，并据此解释性能文档里「取数约 35 分钟」这一反直觉现象的成因。

本讲承接 [u3-l2](./u3-l2-sarbackproject-xrt-init.md)：构造函数已经把三个输入 buffer 申请好并映射成了裸指针，本讲就是「谁来填这些指针、怎么填」。

## 2. 前置知识

阅读本讲前，请确认你已理解以下概念（来自前序讲义）：

- **三类输入数据**（[u1-l2](./u1-l2-repo-structure-and-test-data.md)）：① slowtime，每行 4 列浮点，记录天线 X/Y/Z 位置与场景中心参考距离 `ref_range`；② phdata（RC），每行 `RC_SAMPLES` 列复数，记录距离压缩后的回波样本。两类数据合称「相位历史数据」。
- **`common.h` 宏**（[u1-l4](./u1-l4-common-config-header.md)）：`PULSES=602`（处理的脉冲数=图像行数）、`RC_SAMPLES=512`（距离样本数=图像列数）、`BC_ELEMENTS=4`（slowtime 每行列数）。
- **buffer 映射**（[u3-l2](./u3-l2-sarbackproject-xrt-init.md)）：`xrt::aie::bo` 经 `.map<T*>()` 返回一个指向主机侧内存的裸指针，写这个指针就是写 buffer 的后备内存；之后 `bp()` 会用 GMIO async 把它搬进 AIE。
- **`cfloat`**：AMD ADF/XRT 提供的单精度复数类型，即两个 `float`（共 8 字节），字段名为 `.real` 与 `.imag`。

几个本讲会用到的 C++ 标准库工具，先给一句通俗解释：

| 工具 | 作用 |
|---|---|
| `std::ifstream` | 以文本流方式打开文件，可按行读取。 |
| `std::getline(stream, s, ',')` | 从流中读到指定分隔符（默认换行、这里用逗号）为止，把一段文本存入字符串 `s`。 |
| `std::stringstream` | 把一行字符串当成可读流，配合上面的 `getline` 实现「按列切分」。 |
| `std::regex` / `std::regex_search` / `std::smatch` | 正则匹配；`regex_search` 在字符串里找首个匹配，匹配到的分组放在 `smatch` 里。 |
| `std::stof` | 把字符串（如 `"5.84e-06"`、`"-1.3e-6"`）转成 `float`。 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [design/host/sar_backproject.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp) | 本讲主角。`fetchRadarData()`（L154–L215）解析两类 CSV；构造函数（L20–L63）里申请并映射了本讲要写的 buffer。 |
| [design/host/main.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp) | 在「Populating data buffers」阶段调用 `fetchRadarData()` 并用 `startTime/endTime/printTimeDiff` 计时（L36–L43）。 |
| [design/host/sar_backproject.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h) | 声明 `m_broadcast_data_array`、`m_rc_array` 等成员（L42–L47）。 |
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | `PULSES`、`RC_SAMPLES`、`BC_ELEMENTS` 的定义（L17/L22/L45）。 |
| design/test_data/gotcha_slowtime_pass1_360deg_HH.csv | slowtime 测试数据，每行 4 列浮点。 |
| design/test_data/gotcha_phdata_512-out-of-424-rc-samples_pass1_360deg_HH.csv | RC 测试数据，每行 512 个 `a+bi` 复数。 |

> 真实数据样例（直接取自仓库 CSV）：
>
> - slowtime 第一行：`7089.26464844,0.528879165649,7275.671875,10158.3994141`
> - RC 第一行开头：`5.84543704463e-06-1.3672051864e-06i,2.7613768907e-05-1.42756471178e-06i,...`
>
> 两类文件都含完整 360° 飞行约 42 208 行，但 `fetchRadarData()` 只读前 `PULSES=602` 行。

## 4. 核心概念与源码讲解

### 4.1 `fetchRadarData()`：两类 CSV 的整体解析流程

#### 4.1.1 概念说明

主机拿到的是两个**文本 CSV**，而 AIE 需要的是**定长、紧凑的二进制数组**。`fetchRadarData()` 就是这二者之间的「反序列化器」：逐行读文本 → 切列 → 转成数值 → 写进已经映射好的 device buffer。

它处理两类结构完全不同的数据，因此函数内部分成对称的两段：

1. **slowtime 段**：每行 4 个**纯浮点**，列与列之间用逗号隔开。
2. **RC 段**：每行 512 个**复数**，每个复数自身形如 `a+bi`（虚部符号藏在字段内部），列与列之间仍用逗号隔开。

关键认知是：**slowtime 的「字段」就是一个数；RC 的「字段」是一对数（实部+虚部）打包成 `a+bi`**。这个差异决定了前者只需 `stof`、后者需要正则——这是本讲的核心张力，4.2 节会展开。

#### 4.1.2 核心流程

函数整体可写成下面的伪代码：

```text
fetchRadarData():
    打开 slowtime CSV，失败则返回 1
    pulse_idx = 0
    while 还能读到一行 且 pulse_idx < PULSES:
        把该行按逗号切成 4 个字符串
        依次 stof 后写入 m_broadcast_data_array[BC_ELEMENTS*pulse_idx + 0..3]
        pulse_idx++

    打开 RC CSV，失败则返回 1
    pulse_idx = 0
    while 还能读到一行 且 pulse_idx < PULSES:
        rc_samp_cnt = 0
        while 还能按逗号切出一列 且 rc_samp_cnt < RC_SAMPLES:
            用正则从该列字符串里捕出 real / imag 两组
            stof 后组装成 cfloat，写入 m_rc_array[pulse_idx*RC_SAMPLES + rc_samp_cnt]
            rc_samp_cnt++
        pulse_idx++

    return 0
```

两段共用同一套「按行读 → 按列切」的骨架，差别只在「每一列怎么解析」。

#### 4.1.3 源码精读

函数定义与 slowtime 段：[design/host/sar_backproject.cpp:154-182](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L154-L182) —— 打开文件、`while (std::getline(st_file, line) && pulse_idx < PULSES)` 控制只读前 602 行，每行用 `std::stringstream` + `std::getline(ss, value, ',')` 切出 4 列，分别 `std::stof`。

注意这 4 列是**手写重复**的四次取列（L169–L179），而不是循环：

```cpp
std::getline(ss, value, ',');
this->m_broadcast_data_array[BC_ELEMENTS*pulse_idx]     = std::stof(value); // X
std::getline(ss, value, ',');
this->m_broadcast_data_array[BC_ELEMENTS*pulse_idx + 1] = std::stof(value); // Y
std::getline(ss, value, ',');
this->m_broadcast_data_array[BC_ELEMENTS*pulse_idx + 2] = std::stof(value); // Z
std::getline(ss, value, ',');
this->m_broadcast_data_array[BC_ELEMENTS*pulse_idx + 3] = std::stof(value); // ref_range
```

之所以能展开成 4 次，正是因为 `BC_ELEMENTS` 固定为 4。

调用点与计时：[design/host/main.cpp:36-43](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp#L36-L43) —— `fetchRadarData()` 被「Populating data buffers」阶段的 `startTime/endTime` 包住，返回非 0 时整个程序直接退出。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：把「取数」阶段与其余阶段的时间口径对上。

**操作步骤**：

1. 打开 `main.cpp`，找到 L36–L43 的取数阶段，记下它的计时标签字符串。
2. 对照 [u3-l1](./u3-l1-host-application-flow.md) 的五阶段表，确认 `fetchRadarData()` 对应其中哪一阶段、标签是 `(HOST)` 还是 `(AIE)`。
3. 在 `sar_backproject.cpp` 中确认：`fetchRadarData()` 读取的行数由 `pulse_idx < PULSES` 把关（L164、L191）。

**需要观察的现象**：取数阶段的标签是 `Populating data buffers completed (HOST)`——注意它是 `(HOST)`，意味着这段时间完全花在 ARM CPU 上读文本，AIE 此时尚未真正干活。

**预期结果**：你能用一句话说明「取数慢，是因为 ARM 在解析大文本 CSV，而不是因为数据量大或 AIE 慢」。具体耗时数字待本地验证（性能文档给出的量级见 4.2.4）。

#### 4.1.5 小练习与答案

**练习 1**：slowtime CSV 每行有 4 列，代码却只读了前 `PULSES` 行。slowtime 文件实际有约 42 208 行，剩下的行去哪了？

**参考答案**：被 `pulse_idx < PULSES` 这个循环条件挡掉了。`std::getline` 一行一行读，读到第 602 行后循环退出，文件里其余约 41 606 行根本不会被解析。这正是 u1-l2 提到的「文件含完整 360°，但运行时只取前 `PULSES` 行」。

**练习 2**：如果 slowtime CSV 的某一行只有 3 列（缺一列），代码会怎样？

**参考答案**：第 4 次 `std::getline(ss, value, ',')` 会失败（流到达末尾），`value` 保持上一次的值或为空，`std::stof("")` 会抛 `std::invalid_argument` 异常，程序非正常退出。当前代码没有对每次切列的成功与否做校验——这是初学者阅读时应留意的一处「脆弱点」。

---

### 4.2 复数正则与 real/imag 提取

#### 4.2.1 概念说明

RC 数据的每一列不是普通数字，而是形如 `5.84543704463e-06-1.3672051864e-06i` 的**复数**：

- 实部 `5.84543704463e-06`
- 虚部系数 `-1.3672051864e-06`（连带它前面的负号）
- 后缀字母 `i`

问题在于：**这个字段里没有逗号**，实部和虚部是用中间的 `+`/`-` 号「粘」在一起的。`std::stof` 没办法直接吃掉整个 `...e-06-1.3...e-06i`（遇到 `i` 和内部符号就会出错或截断）。所以必须用一个正则，把实部和虚部系数当作两个分组分别「抠」出来，再各自 `stof`。

这就是「为什么 RC 要用正则、slowtime 不用」的根因——slowtime 的字段就是一个孤立数字，`stof` 足矣；RC 的字段是「两个数字打包」，必须先拆包。

#### 4.2.2 核心流程

用一个正则把 `a±bi` 拆成两段的流程：

```text
对每个 RC 字符串 value（如 "5.84e-06-1.36e-06i"）:
    regex_search(value, match, complex_regex)
        match[1] 命中实部  → "5.84543704463e-06"
        match[2] 命中虚部系数（含符号）→ "-1.3672051864e-06"
        末尾的字面量 'i' 不进分组，仅作锚点
    real_part = stof(match[1])   // 5.845e-06f
    imag_part = stof(match[2])   // -1.367e-06f
    组装 cfloat{real_part, imag_part}
```

#### 4.2.3 源码精读

正则与解析：[design/host/sar_backproject.cpp:197-209](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L197-L209)。核心三行：

```cpp
std::regex complex_regex(R"(([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([+-](?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)i)");
std::smatch match;
std::regex_search(value, match, complex_regex);
float real_part = std::stof(match[1].str());
float imag_part = std::stof(match[2].str());
```

那串看起来吓人的正则其实只做两件事——把「一个可选符号的数」和「一个必带符号的数」分别捕获。拆开看（`R"(...)"` 是 C++ 原始字符串，免去反斜杠转义）：

```text
(                              # 第 1 组：实部
  [+-]?                        #   可选符号（字段开头，可有可无）
  (?:\d+(?:\.\d*)?|\.\d+)      #   尾数：如 5 / 5. / 5.84 / .5
  (?:[eE][+-]?\d+)?            #   可选指数：如 e-06
)
(                              # 第 2 组：虚部系数
  [+-]                         #   必选符号！这就是 a+bi 里的 +/-
  (?:\d+(?:\.\d*)?|\.\d+)      #   同样的尾数
  (?:[eE][+-]?\d+)?            #   同样的可选指数
)
i                              # 字面量 i，锚定这是个虚数，不进分组
```

把它对到真实样本 `5.84543704463e-06-1.3672051864e-06i` 上：

| 分组 | 命中字符串 | `stof` 后 |
|---|---|---|
| `match[1]`（实部） | `5.84543704463e-06` | `5.845437e-06f` |
| `match[2]`（虚部系数） | `-1.3672051864e-06` | `-1.3672052e-06f` |

两个值得记住的细节：

- **为什么虚部符号是必选 `[+-]`、实部是可选 `[+-]?`**：在 `a+bi` 这种紧凑写法里，虚部系数前的 `+`/`-` 同时承担了「连接符」的作用，没有它就无法把实部与虚部切开；而实部是整个字段的开头，可能带负号也可能不带。换句话说，这个 `+`/`-` 既是数值符号、也是两个数之间的分隔，正则正好利用它来定位第 2 组。
- **为什么用 `regex_search` 而非 `regex_match`**：`match` 要求整串完全吻合，`search` 只要在串里找到一处匹配即可，对前后可能的空白更宽容。这里每个 `value` 就是一个复数，两者都能工作，作者选了更宽松的 `search`。

精度提示：CSV 里写了约 12 位有效数字，但 `std::stof` 返回 32 位 `float`（约 7 位有效数字），所以会有舍入。对本设计的雷达复数样本而言这点精度损失可接受。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：亲手验证「为什么 RC 要正则、slowtime 不要」，并量化「数据本身」与「取数耗时」之间的巨大落差。

**操作步骤**：

1. **解释正则的必要性**。写一段说明（一两段话即可）：
   - 取一个 slowtime 字段（如 `7089.26464844`）和一个 RC 字段（如 `5.84543704463e-06-1.3672051864e-06i`）。
   - 论证前者直接 `std::stof` 就能解析，而后者不行——指出 `i` 后缀和藏在中间的虚部符号会让 `stof` 无法把整个字段当作一个数。
   - 由此说明：正则的职责是「把一个 `a+bi` 字段拆成两个可被 `stof` 吃掉的数字」。

2. **估算 RC buffer 在 DDR 中的字节数**。用默认宏 `PULSES=602`、`RC_SAMPLES=512`、`sizeof(cfloat)=8` 计算：

   \[
   \text{RC buffer 字节} = 602 \times 512 \times 8 = 2\,468\,096 \;\text{B} \approx 2.35\,\text{MiB}
   \]

   作为对照，slowtime buffer 只有 \(602 \times 4 \times 4 = 9\,632\) B ≈ 9.4 KiB，可忽略不计。

3. **解释「取数 35 分钟」的成因**。打开 [doc/sections/performance_metrics.tex:131-134](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/performance_metrics.tex#L131-L134)，原文指出该阶段约耗时 35 分钟，「due to loading the slow time and range-compressed samples into DDR from a large CSV file」。结合你上面的估算回答：RC 数据真正落进 DDR 的只有约 2.5 MiB，搬运这点数据本身不可能花 35 分钟；瓶颈在于 ARM 用 `std::regex_search` + `std::stof` 逐 token 解析大文本 CSV——仅 RC 段就要做 \(602 \times 512 = 308\,224\) 次正则搜索与约 61.6 万次 `stof`，而 `std::regex` 在 C++ 里以慢著称。原文紧接着说「During normal operation, this data would be streamed in through a high-speed interface」，正说明：一旦数据以二进制流方式直达，这段文本解析就会消失。

**需要观察的现象 / 预期结果**：你会得到一个反直觉但很重要的结论——**取数阶段的「慢」与数据规模几乎无关，与「文本解析」强相关**；真正的星载运行不会读 CSV，这条路径只是地面验证用。

> 待本地验证：若你在 VCK190 上实跑，可在 `main.cpp` L43 的打印里看到「Populating data buffers」的真实毫秒数，与性能文档的量级核对。

#### 4.2.5 小练习与答案

**练习 1**：若 RC 字段是 `3.0+4.0i`，`match[1]`、`match[2]` 分别捕获到什么？最终存成什么 `cfloat`？

**参考答案**：`match[1] = "3.0"`，`match[2] = "+4.0"`；`stof` 后 `real_part=3.0f`、`imag_part=4.0f`；存为 `cfloat{3.0f, 4.0f}`。

**练习 2**：把虚部符号从必选 `[+-]` 改成可选 `[+-]?` 会导致什么问题？

**参考答案**：正则会失去「实部与虚部分界」的锚点。例如 `5.84e-06-1.36e-06i` 可能被错误地匹配成实部 `5`、虚部 `.84e-06`（把后面当成另一个数），因为缺少强制符号约束后，分组边界变得含糊。当前的必选符号正是为了保证「第 2 组从一个 `+`/`-` 开始」。

**练习 3**：RC 内层循环为什么是 `while (std::getline(ss, value, ',') && rc_samp_cnt < RC_SAMPLES)`，两个条件缺一不可吗？

**参考答案**：缺一不可。`getline` 负责「按逗号真正切出下一列」，`rc_samp_cnt < RC_SAMPLES` 负责「写数组时不越界并保持每行恰好 512 个」。若某行 CSV 意外多写了列，`rc_samp_cnt` 守卫会忽略多余的；若少写了，`getline` 会先失败退出。二者共同保证 `m_rc_array` 的行步长恒为 `RC_SAMPLES`。

---

### 4.3 写入映射后的 device buffer 数组

#### 4.3.1 概念说明

解析得到的数值，最终要落到两个「**已经映射成裸指针**」的数组里：`m_broadcast_data_array`（`float*`）与 `m_rc_array`（`cfloat*`）。这两个指针来自 [u3-l2](./u3-l2-sarbackproject-xrt-init.md) 里构造函数的 `.map<>()` 调用：

- `m_broadcast_data_array = m_broadcast_data_buffer.map<float*>()` —— [design/host/sar_backproject.cpp:35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L35)
- `m_rc_array = m_rc_buffer.map<cfloat*>()` —— [design/host/sar_backproject.cpp:39](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L39)

写入这些指针，就是在写 buffer 的**主机侧后备内存**。本阶段结束后，数据还停留在主机侧；真正把它们搬进 AIE，是后续 `bp()` 里 `XCL_BO_SYNC_BO_GMIO_TO_AIE` 的职责（见 [u3-l5](./u3-l5-orchestrating-aie-and-pl.md)）。

#### 4.3.2 核心流程

两个数组的布局都是**行优先（row-major）**，一行对应一个脉冲：

```text
m_broadcast_data_array  (float*，长度 PULSES*BC_ELEMENTS):
    [pulse 0: X0, Y0, Z0, ref_range0,
     pulse 1: X1, Y1, Z1, ref_range1,
     ... ]
    索引：BC_ELEMENTS*pulse_idx + col        (col ∈ 0..3)

m_rc_array  (cfloat*，长度 PULSES*RC_SAMPLES):
    [pulse 0: rc[0..511],
     pulse 1: rc[0..511],
     ... ]
    索引：pulse_idx*RC_SAMPLES + rc_samp_cnt (rc_samp_cnt ∈ 0..511)
```

这种「行 = 脉冲，列 = 距离样本」的排布，恰好与输出图像的「行 = 脉冲，列 = 距离」一致，也决定了 `bp()` 后续能「逐脉冲」地把 RC 数据整块喂进 AIE。

#### 4.3.3 源码精读

slowtime 写入（[design/host/sar_backproject.cpp:170-179](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L170-L179)）：4 列依次写入 `BC_ELEMENTS*pulse_idx + {0,1,2,3}`，刚好对应 X/Y/Z/ref_range。

RC 写入（[design/host/sar_backproject.cpp:206](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L206)）：

```cpp
this->m_rc_array[pulse_idx*RC_SAMPLES + rc_samp_cnt] = (cfloat) {real_part, imag_part};
```

`(cfloat) {real, imag}` 是用聚合初始化给 `cfloat` 的 `.real`、`.imag` 两个字段赋值。

buffer 大小在构造函数里就已按同样的宏算好（[design/host/sar_backproject.cpp:34-39](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L34-L39)）：

```cpp
m_broadcast_data_buffer(m_device, PULSES*BC_ELEMENTS*sizeof(float), ...)
...
m_rc_buffer(m_device, PULSES*RC_SAMPLES*sizeof(cfloat), ...)
```

因此「写索引」与「buffer 容量」基于同一组宏，二者天然自洽，不会越界。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：把「写入端」与「消费端」对上，确认数据没有写错地方。

**操作步骤**：

1. 在本讲确认写入索引：slowtime 用 `BC_ELEMENTS*pulse_idx+col`，RC 用 `pulse_idx*RC_SAMPLES+rc_samp_cnt`。
2. 打开 `bp()`（[design/host/sar_backproject.cpp:279-335](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L279-L335)），找到它如何把 `m_rc_buffer` 喂进 AIE：
   - 注意 L294–L297：每个脉冲 `async(... gmio_in_rc ..., RC_SAMPLES*sizeof(cfloat), (pulse_idx*RC_SAMPLES)*sizeof(cfloat))`。
3. 对照本讲的写入索引与 `bp()` 里的偏移 `(pulse_idx*RC_SAMPLES)*sizeof(cfloat)`，确认二者用的是**同一套行优先布局**。

**需要观察的现象**：`bp()` 逐脉冲投递时，偏移正好指向本讲写入的第 `pulse_idx` 行，说明「写」和「读」对同一块内存的理解完全一致。

**预期结果**：你能画出一条链路——CSV 文本 → `stof`/正则 → `m_rc_array[pulse_idx*RC_SAMPLES+...]` → （`bp()` 中）GMIO async 从同一偏移搬进 AIE。

#### 4.3.5 小练习与答案

**练习 1**：`m_broadcast_data_array` 与 `m_rc_array` 的元素类型分别是什么？为什么不一样？

**参考答案**：前者是 `float*`（slowtime 每列就是一个实数），后者是 `cfloat*`（RC 每列是一个复数）。类型差异直接源于两类数据的物理含义不同：天线几何坐标是实数，回波样本是复数（含幅度与相位）。

**练习 2**：构造函数里 `m_rc_buffer` 的大小写成 `PULSES*RC_SAMPLES*sizeof(cfloat)`，而 `fetchRadarData()` 写数组的最大下标是 `(PULSES-1)*RC_SAMPLES + (RC_SAMPLES-1)`。这两者一致吗？

**参考答案**：一致。最大下标 \(= (\text{PULSES}-1)\cdot\text{RC\_SAMPLES} + (\text{RC\_SAMPLES}-1) = \text{PULSES}\cdot\text{RC\_SAMPLES} - 1\)，正好是「元素总数 − 1」，落在容量 `PULSES*RC_SAMPLES` 个 `cfloat` 之内。写索引与容量同源，故不会越界。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「**最小复刻 fetchRadarData 的两段解析器**」任务。

**任务**：用 Python（或你熟悉的语言）写一个小脚本，对下面这三行「迷你数据」分别复刻 slowtime 与 RC 的解析逻辑，并打印解析后的数组。

```
# mini_slowtime.csv（每行 4 列纯浮点）
7089.26464844,0.528879165649,7275.671875,10158.3994141
7089.26074219,1.58422386646,7275.67333984,10158.3974609

# mini_rc.csv（每行 3 个复数，注意 a+bi 打包）
5.84543704463e-06-1.3672051864e-06i,2.7613768907e-05-1.42756471178e-06i,3.0+4.0i
1.0e-01+2.0e-01i,-3.5e+00-1.2e-01i,0.0+0.0i
```

**要求**：

1. slowtime 段：按逗号切列，直接转 `float`，存成长度为 `行数*4` 的一维数组，索引规则用 `4*row + col`（对齐 `BC_ELEMENTS*pulse_idx+col`）。
2. RC 段：对每列用一个等价正则（Python 可用 `([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([+-](?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)i`）抽实部、虚部，组装成 `(real, imag)`，存成 `行数*3` 的复数数组，索引规则用 `3*row + col`（对齐 `pulse_idx*RC_SAMPLES+rc_samp_cnt`）。
3. 验证：第二行第三列 `3.0+4.0i` 解析为 `(3.0, 4.0)`；`0.0+0.0i` 解析为 `(0.0, 0.0)`，确认正则对「零」与「正号虚部」也能正确命中。

**预期结果**：你会直观看到——slowtime 段根本不需要正则（切列即可），RC 段没正则就拆不开 `a+bi`。这正是 4.2 节的核心结论，亲手实现一遍比读十遍更牢。

> 示例代码（Python）：

```python
# 示例代码（非项目原有代码）
import re
CX = re.compile(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([+-](?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)i')

def parse_slowtime(lines, BC=4):
    arr = [0.0] * (len(lines) * BC)
    for r, line in enumerate(lines):
        cols = line.split(',')
        for c in range(BC):
            arr[BC*r + c] = float(cols[c])      # 对齐 m_broadcast_data_array
    return arr

def parse_rc(lines, RC=3):
    arr = [(0.0, 0.0)] * (len(lines) * RC)
    for r, line in enumerate(lines):
        for c, token in enumerate(line.split(',')[:RC]):
            m = CX.search(token)
            arr[RC*r + c] = (float(m.group(1)), float(m.group(2)))  # 对齐 m_rc_array
    return arr
```

## 6. 本讲小结

- `fetchRadarData()` 是「文本 CSV → 紧凑二进制数组」的反序列化器，分 slowtime 与 RC 对称两段，共用「按行读、按逗号切列」的骨架。
- slowtime 每行 4 个**纯浮点**，直接 `std::stof` 即可；RC 每行 512 个**复数**，每个复数是 `a+bi` 打包，必须先正则拆包。
- 复数正则用两个分组分别捕获实部（可选符号）与虚部系数（**必选符号**，充当实虚部分界），末尾字面量 `i` 作锚点；`regex_search` 比 `match` 更宽容。
- 解析结果按**行优先**写入映射指针：`m_broadcast_data_array[BC_ELEMENTS*pulse_idx+col]` 与 `m_rc_array[pulse_idx*RC_SAMPLES+rc_samp_cnt]`，写索引与构造函数里的 buffer 容量同源、不会越界。
- RC buffer 实际只占约 **2.35 MiB** DDR，性能文档里「取数约 35 分钟」的瓶颈是 ARM 上逐 token 的正则 + `stof` 文本解析（仅 RC 段就约 30.8 万次正则搜索），而非数据规模；真实星载运行改用高速接口流式输入即可消除该开销。

## 7. 下一步学习建议

- 数据进了 buffer 之后，下一步是「**算出要成像的目标像素网格**」。这正是 [u3-l4 目标像素生成与方位角解卷绕](./u3-l4-target-pixels-and-unwrapping.md) 的主题：`genTargetPixels()` 会消费本讲填好的 `m_broadcast_data_array`，用 `atan2` + `unwrap` 算方位分辨率，生成第三个输入 buffer `m_xyz_px_array`。
- 如果你想提前看清这些 buffer 如何被搬进 AIE，可跳读 [u3-l5 用 XRT 编排 AIE 图与 PL 内核](./u3-l5-orchestrating-aie-and-pl.md) 中 `bp()` 的 GMIO async 段，重点看它如何用本讲写入的同一套行优先偏移逐脉冲投递数据。
- 建议顺便阅读 `design/host/sar_backproject.cpp` 中 `writeImg()`（L135–L152），它是「反向」操作：把 AIE 算完的 `m_img_arrays` 复数数组重新写回 `a+bi` 文本 CSV，与本讲的「读 CSV」正好对称，有助于你完整理解主机侧的 I/O 约定。
