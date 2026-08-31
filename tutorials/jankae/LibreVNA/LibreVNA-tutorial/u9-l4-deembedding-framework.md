# 去嵌入框架与选项体系

## 1. 本讲目标

上一讲（u9-l2）我们弄清楚了校准求解器：它用 SOLT 等流程解出 12 项误差模型，把仪器自身的误差从测量中扣除。但校准的参考面止步于测量电缆的校准端面——如果你在被测件（DUT）前面还串了一段夹具、一个衰减器或一个匹配网络，校准后的数据依然"夹带"着这些装置的影响。

**去嵌入（De-embedding）**就是在已校准数据之上，再把这些已知（或可建模）的外围电路"从数学上拆掉"的过程。LibreVNA 把这件事做成了一个**插件式框架**：一个抽象基类 `DeembeddingOption` 定义契约，一个容器类 `Deembedding` 管理选项列表，每个具体选项（端口延伸、2x-Thru、匹配网络、阻抗再归一化）各自实现数学变换。

学完本讲，你应该能够：

1. 说出 `DeembeddingOption` 的接口约定：哪些函数必须实现、`edit()` 与 `measurementCompleted()` 何时被调用、选项在什么情况下允许"自毁"。
2. 准确指出去嵌入在整个测量管线中的位置——它作用在**平均之后、校准之后**的数据上，结果写入 Trace 的第二份独立数据集。
3. 独立编写并注册一个最简去嵌入选项（理想衰减器），让它在设置对话框、SCPI 命令树和 `.setup` 持久化中自动可用。

## 2. 前置知识

### 2.1 去嵌入与校准的分界

- **校准（Calibration）**：修正*仪器自身*的误差（方向性、跟踪、匹配等），参考面是校准件所在的端面。上一讲的 12 项误差模型干的就是这件事。
- **去嵌入（De-embedding）**：修正*仪器之外、DUT 之前*的已知网络（夹具、转接器、衰减器、匹配网络）。它假设输入数据已经是校准后的 S 参数。

两者的数学形式其实一样——都是"除以一个已知网络的贡献"——但职责正交：校准让仪器"说真话"，去嵌入把真话"挪到 DUT 自己的参考面上"。

### 2.2 级联网络的对偶：乘出来与除回去

若 DUT 前串了一个衰减系数为 \(k\)（\(0<k\le 1\)，\(k=10^{-a/20}\)，\(a\) 为衰减量 dB 值）的装置，测量到的传输参数是：

\[ S_{21}^{\text{meas}} = S_{21}^{\text{DUT}} \cdot k \]

要恢复 DUT 的真实值，只需除回去：

\[ S_{21}^{\text{DUT}} = \frac{S_{21}^{\text{meas}}}{k} = S_{21}^{\text{meas}} \cdot 10^{a/20} \]

用 dB 表示更直观：测量的 dB 值加上 \(a\) dB。本讲综合实践的理想衰减器就是这个一维特例；端口延伸（相位旋转 + 平方根损耗模型）、2x-Thru（完整双端口网络求解）只是把 \(k\) 换成更复杂的频率函数。

### 2.3 需要回忆的前置结论

- **u3-l1**：`DeviceDriver::VNAMeasurement` 是硬件无关的测量点，`measurements` 是以 `"S11"`、`"S21"` 这类字符串为键、线性复数为值的 map，另有 `frequency`（Hz）与 `pointNum`（点号）。
- **u7-l1**：VNA 模式的数据上行链路是 `NewDatapoint` 槽 → 平均 → 定型 X 轴 → 校准 → 写入 TraceModel。
- **u9-l2**：`cal.correctMeasurement(m_avg)` 就地修正一个测量点，校准后的数据才是本讲框架的输入。
- **u2-l2 / u9-l1**：`Mode`、`Calkit` 都是 `QObject + Savable + SCPINode` 三重继承——本讲的 `DeembeddingOption` 沿用了完全相同的"一个对象同时是界面元素、可持久化对象、SCPI 节点"手法。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [VNA/Deembedding/deembeddingoption.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingoption.h) | 抽象基类：选项契约、Type 枚举、类型名转换 |
| [VNA/Deembedding/deembeddingoption.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingoption.cpp) | 静态工厂 `create()` 与类型字符串互转 |
| [VNA/Deembedding/deembedding.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.h) / [.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.cpp) | 选项容器：增删换序、逐点变换、测量调度、SCPI 子树、JSON 持久化 |
| [VNA/Deembedding/deembeddingdialog.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingdialog.cpp) | 设置对话框与选项列表模型（OptionModel） |
| [VNA/Deembedding/portextension.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp) | 参照实现：端口延伸（本讲实践的模板） |
| [VNA/Deembedding/manualdeembeddingdialog.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/manualdeembeddingdialog.cpp) | 对已有 Trace（如导入数据）手动去嵌入的入口 |
| [VNA/vna.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp) | 管线衔接点：NewDatapoint 的调用顺序、菜单、测量启停联动 |
| [Traces/trace.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp) | Trace 的"双数据集"：`data` 与 `deembeddingData` |
| [LibreVNA-GUI.pro](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro) | 工程文件：新增源码必须在此登记 |

（下文路径均省略前缀 `Software/PC_Application/LibreVNA-GUI/`。）

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**DeembeddingOption 接口**、**选项管理对话框**、**与校准管线的衔接**。

### 4.1 DeembeddingOption 接口

#### 4.1.1 概念说明

`DeembeddingOption` 是"一种去嵌入算法"的抽象。它回答三个问题：

1. **我影响哪些端口？**（`getAffectedPorts`）——容器据此决定测量时需要激励哪些端口、测量对话框里允许选择哪些 Trace。
2. **我如何变换一个测量点？**（`transformDatapoint`）——纯数学核心，就地修改传入的 `VNAMeasurement`。
3. **我是什么类型？**（`getType`）——用于工厂创建、界面显示与持久化身份。

除此之外还有两个"可选能力"：

- `edit()`：打开编辑对话框（默认空实现——一个不需要参数的选项可以什么都不写）。
- `measurementCompleted(m)`：接收一次完整扫描的测量数据（默认空实现——纯参数型选项如阻抗再归一化不需要测量）。

需要测量的选项（如端口延伸的自动提取）通过 `triggerMeasurement()` 信号向容器"预约"一次扫描，结果异步送回 `measurementCompleted()`。选项还可能在运行中发现自己不适用（例如端口数不够），此时可以发出 `deleted` 信号后自我删除——容器会把它从列表里摘掉。

#### 4.1.2 核心流程

一个选项从创建到生效的伪代码：

```
DeembeddingDialog 里点击 "Add" → 某类型
  → DeembeddingOption::create(type)        # 工厂 new 出具体子类
  → Deembedding::addOption(option)         # 入列、接信号、编 SCPI 号
  → option->edit()                         # 弹出参数对话框（若有）
     └─ 用户点击自动测量按钮
        → emit triggerMeasurement()        # 向容器预约扫描
           （容器接住后开启测量回路，见 4.3）
之后每个测量点流过：
  → option->transformDatapoint(p)          # 就地修改，无返回值
保存工作区时：
  → toJSON() 记录 {类型名 + 参数}
```

类型身份的解析路径：

```
Type 枚举值  ←→  显示/存储字符串（TypeToString / TypeFromString）
   例：Type::PortExtension  ←→  "Port Extension"
       Type::TwoThru        ←→  "2xThru"
```

这个字符串同时是 UI 菜单上的名字、`toJSON` 里 `operation` 字段的值和 SCPI `NEW` 命令的参数——一处定义，三处消费。

#### 4.1.3 源码精读

**类声明与三重继承**。[deembeddingoption.h:L11-L15](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingoption.h#L11-L15)：与 `Mode`、`Calkit` 一致的 `QObject + Savable + SCPINode` 组合，使每个选项天生就是一个可挂参数的 SCPI 节点。

**Type 枚举与扩展点注释**。[deembeddingoption.h:L16-L23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingoption.h#L16-L23)：

```cpp
enum class Type {
    PortExtension,
    TwoThru,
    MatchingNetwork,
    ImpedanceRenormalization,
    // Add new deembedding options here, do not explicitly assign values and keep the Last entry at the last position
    Last,
};
```

注释就是官方给出的扩展规约：**新类型插在 `Last` 之前、不显式赋值**。`Last` 既是"循环上界"（枚举遍历 `0..Last-1`）也是 `TypeFromString` 的失败返回值——所有依赖枚举遍历的代码（对话框菜单、SCPI 类型查询、JSON 反序列化）都自动纳入新类型，无需逐处修改。

**三个纯虚函数**。[deembeddingoption.h:L29-L32](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingoption.h#L29-L32)：`getAffectedPorts`、`transformDatapoint`、`getType` 是必须实现的契约；`edit()` 在 L31 有默认空体。`transformDatapoint` 接收的是引用且无返回值——它必须**就地**修改测量点，这决定了多个选项天然按列表顺序"接力"处理同一份数据。

**信号与槽**。[deembeddingoption.h:L34-L40](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingoption.h#L34-L40)：`measurementCompleted` 是虚槽（默认丢弃数据），`deleted` 与 `triggerMeasurement` 两个信号的注释分别说明了自毁协议和测量预约协议。

**受保护的构造函数**。[deembeddingoption.h:L42-L44](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingoption.h#L42-L44)：子类构造时必须传一个 SCPI 短名（如端口延伸传 `"PORTEXTension"`），选项将以这个名字挂在 SCPI 命令树下。

**工厂与类型字符串**。[deembeddingoption.cpp:L8-L22](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingoption.cpp#L8-L22) 的 `create()` 用 switch 把枚举映射到 `new PortExtension()` 等具体类；[L24-L38](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingoption.cpp#L24-L38) 的 `TypeToString` 给出人类可读名；[L40-L48](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingoption.cpp#L40-L48) 的 `TypeFromString` 遍历枚举做大小写不敏感匹配，找不到返回 `Last`。

**参照实现：端口延伸的数学核心**。[portextension.cpp:L41-L61](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp#L41-L61)：

```cpp
auto phase = -2 * M_PI * ext.delay * d.frequency;
auto db_attennuation = ext.DCloss;
if(ext.frequency != 0) {
    db_attennuation += ext.loss * sqrt(d.frequency / ext.frequency);
}
auto att = pow(10.0, -db_attennuation / 20.0);
auto correction = polar<double>(att, phase);
for(auto &m : d.measurements) {
    if(m.first.mid(1, 1).toUInt() == port) { m.second /= correction; }
    if(m.first.mid(2, 1).toUInt() == port) { m.second /= correction; }
}
```

这段 20 行代码浓缩了端口延伸的全部物理。夹具（一段传输线）对频率 \(f\) 信号的贡献是：

\[ c(f) = 10^{-\frac{a_{\text{DC}} + a_1\sqrt{f/f_1}}{20}} \cdot e^{-j2\pi f\tau} \]

其中损耗项用 \(\sqrt{f}\) 模型近似导体趋肤效应，\(\tau\) 是单向时延。去嵌入即除以 \(c(f)\)。真正精妙的是两个**并列**（非 else-if）的 if：键 `"S21"` 的 `mid(1,1)` 是接收端口下标、`mid(2,1)` 是激励端口下标。反射参数 S11（收、发都是 `port`）会**命中两次**，除以 \(c^2\)——因为反射信号来回穿越夹具两次；传输参数 S21（只有一端是 `port`）命中一次，除以 \(c\) 一次。两个 if 各对应信号对该端口夹具的一次穿越，物理上严格正确。

**SCPI 参数直接挂在选项节点上**。[portextension.cpp:L29-L33](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp#L29-L33)：`addUnsignedIntParameter("PORT", port)` 等调用把成员变量注册为 SCPI 可读写参数，于是 `:VNA:DEEMBedding:1:PORTEXTension:DELAY` 这样的远程调参不需要写任何额外代码（SCPI 细节见 u10-l1）。这也是本讲实践中调参数的捷径之一。

**持久化**。[portextension.cpp:L224-L252](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp#L224-L252)：`toJSON`/`fromJSON` 只存参数；注意 `fromJSON` 里对旧格式的兼容分支（无 `port` 键则取 `j[0]` 且端口固定为 1）。

#### 4.1.4 代码实践

**实践目标**：不动手写代码，先从接口反推"接入一个新选项需要做什么"，形成清单（综合实践将照此执行）。

**操作步骤**：

1. 只打开 [deembeddingoption.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingoption.h) 与 [LibreVNA-GUI.pro](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro) 两个文件，写出清单。
2. 用 Grep 在仓库中搜 `Type::Last`，观察哪些地方以它为循环上界遍历全部类型（提示：deembeddingoption.cpp 的 `TypeFromString`、deembedding.cpp 的 `fromJSON`、deembeddingdialog.cpp 的菜单生成）。

**需要观察的现象**：遍历 `0..Last-1` 的代码是否都不需要为新增类型修改；`switch(type)` 的工厂是否是唯一必须改的分支处。

**预期结果**：清单应为四类改动——

| 改动点 | 文件 | 必须？ |
| --- | --- | --- |
| 新建子类头/源文件并登记到 .pro | LibreVNA-GUI.pro L147-154（HEADERS）、L305-312（SOURCES） | 是 |
| Type 枚举加一项（`Last` 之前） | deembeddingoption.h | 是 |
| `create()` 加 case | deembeddingoption.cpp | 是 |
| `TypeToString()` 加 case | deembeddingoption.cpp | 是 |
| 对话框菜单、SCPI `NEW`、JSON 加载 | 无需改动（枚举遍历自动生效） | 否 |

（此表为"源码阅读型实践"的推断结论，将在第 5 节综合实践中实际验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `transformDatapoint` 设计成就地修改引用，而不是接收一个点、返回一个新点？

**答案**：就地修改让多个选项可以按列表顺序对**同一份数据接力**处理，避免每个选项复制一次整个 map 的开销；同时它天然定义了语义——第 n 个选项看到的是前 n−1 个选项已经处理过的数据。这也解释了为什么对话框要提供"上移/下移"改变顺序：顺序不同，结果可能不同（例如先做阻抗再归一化再做端口延伸，与相反顺序的物理意义不同）。

**练习 2**：端口延伸的 `transformDatapoint` 中，若把两个 if 改成 `else if`，会对 S11 的修正造成什么错误？

**答案**：S11 只会命中第一个 if、除以 \(c\) 一次而不是 \(c^2\)，即只扣除了一半的夹具效应（时延和损耗都只修正一半），反射参数的相位斜率与损耗斜率都会偏差一倍。传输参数不受影响。

**练习 3**：一个新选项的 `TypeToString` 返回空字符串 `""` 会发生什么？

**答案**：`TypeFromString` 匹配不到任何非空输入，返回 `Last`；于是 SCPI `NEW` 命令与 `fromJSON` 加载 .setup 时都无法创建该选项（后者还会打印 "Unable to create de-embedding operation" 警告）；对话框菜单里则会出现一个空白菜单项。类型字符串是选项的持久化身份，不能为空。

### 4.2 选项管理：Deembedding 容器与设置对话框

#### 4.2.1 概念说明

`Deembedding` 类是选项的"管家"，它自己不做任何数学，只负责：

- **列表管理**：`addOption` / `removeOption` / `swapOptions` / `clear`；
- **数据分发**：`Deembed(d)` 把每个流过的测量点依次交给所有选项变换；
- **测量调度**：替某个选项预约一次完整扫描，收集数据后回调；
- **三重身份**：SCPI 节点（`DEEMBedding` 子树）、Savable（.setup 持久化）、以及通过 `configure()` 槽弹出设置对话框。

`DeembeddingDialog` 则是纯 UI：一个列表（`OptionModel`）加"添加（下拉菜单）/删除/上移/下移/编辑"按钮。它的关键设计是**添加菜单由枚举遍历自动生成**——新增选项类型后对话框代码零改动。

#### 4.2.2 核心流程

逐点变换（在线去嵌入的核心循环）：

```
Deembed(VNAMeasurement &d):
    for option in options:            # 按列表顺序
        if 正在为 option 测量:
            按 pointNum 收集 d 到 measurements
            若是最后一点 → 结束测量、回调 option
        option->transformDatapoint(d) # 每个选项都执行（含正在测量的）
```

选项的增删联动（VNA 侧菜单状态）：

```
addOption    → emit optionAdded       → VNA 勾选 "De-embed VNA samples"
列表清空     → emit allOptionsCleared → VNA 取消勾选并禁用相关菜单
```

#### 4.2.3 源码精读

**核心循环与测量收集**。[deembedding.cpp:L162-L190](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.cpp#L162-L190)：`Deembed(DeviceDriver::VNAMeasurement &d)` 遍历 `options`，对每个选项：若它正是 `measuringOption` 且 `measuring` 为真，则按 `pointNum` 收集——`pointNum == 0` 且尚无数据时记下第一点，之后逐点追加，直到 `pointNum == sweepPoints - 1` 宣告测量完成（发出 `finishedMeasurement` 并调用 `measurementCompleted()`）；随后无条件调用 `(*it)->transformDatapoint(d)`。注意收集发生在该选项自己变换**之前**：一个选项拿到的测量数据，是"排在她前面的选项已处理、她自己尚未处理"的状态。

**手动去嵌入重载**。[deembedding.cpp:L192-L205](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.cpp#L192-L205)：`Deembed(std::map<QString, Trace*>)` 面向已经存在的 Trace（典型是导入的 Touchstone）：先用 `Trace::assembleDatapoints` 把一组 S 参数 Trace 拼回数据点，逐点走同一个 `Deembed(p)`，再由 `Trace::fillFromDatapoints(traceSet, points, true)` 写回去嵌入数据集，最后对每条 Trace `setDeembeddingActive(true)` 切换显示。这是**无硬件验证去嵌入选项的官方路径**，第 5 节实践将用到它。

**addOption 的两个信号连接**。[deembedding.cpp:L219-L235](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.cpp#L219-L235)：入列后连接 `deleted`（选项自毁时从列表摘除）与 `triggerMeasurement`（记录 `measuringOption` 并打开测量对话框），随后 `updateSCPINames()` 重编 SCPI 号、发出 `optionAdded`。

**SCPI 号重编的细节**。[deembedding.cpp:L80-L93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.cpp#L80-L93)：`updateSCPINames` 先把所有选项从 SCPI 节点摘下、再按 1..n 重新挂载。注释解释了为什么要"先全摘再全挂"——若边摘边挂，重编号过程中会出现两个选项短暂同名（例如两个都叫 "1"）导致改名失败。选项的 SCPI 地址因此是**位置而非身份**：`:VNA:DEEMBedding:3:...` 永远指列表第 3 项，交换顺序后含义随之改变。

**SCPI 管理命令**。[deembedding.cpp:L104-L159](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.cpp#L104-L159)：构造函数注册了 `NUMber`（查询选项数）、`TYPE`（按 1 起的序号查类型，空格替换为下划线）、`NEW`（按类型名创建）、`DELete`、`SWAP`、`CLEAR` 六条命令——整套增删换序操作都可以远程完成。

**持久化格式**。[deembedding.cpp:L273-L283](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.cpp#L273-L283)：`toJSON` 产出 `[{operation: "Port Extension", settings: {...}}, ...]`——按顺序记录"类型名 + 各自参数"。[L285-L318](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.cpp#L285-L318) 的 `fromJSON` 清空后逐项按 `operation` 字符串反查类型重建，未知类型跳过并告警（向前兼容：旧 setup 里已删除的选项不会导致加载失败）。

**自动生成的添加菜单**。[deembeddingdialog.cpp:L15-L25](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingdialog.cpp#L15-L25)：

```cpp
auto addMenu = new QMenu();
for(unsigned int i=0;i<(unsigned int)DeembeddingOption::Type::Last;i++) {
    auto type = (DeembeddingOption::Type) i;
    auto action = new QAction(DeembeddingOption::TypeToString(type));
    connect(action, &QAction::triggered, [=](){
        auto option = DeembeddingOption::create(type);
        model.addOption(option);
    });
    addMenu->addAction(action);
}
ui->bAdd->setMenu(addMenu);
```

循环上界是 `Type::Last`，菜单项文字来自 `TypeToString`——这是"新增类型零 UI 改动"的直接证据。

**OptionModel：列表模型与自动编辑**。[deembeddingdialog.cpp:L95-L102](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingdialog.cpp#L95-L102)：`addOption` 用 `beginInsertRows`/`endInsertRows` 包住 `d->addOption(option)`（Qt 模型协议，通知视图刷新），随后**立即调用 `option->edit()`**——新添加的选项马上弹出参数对话框，引导用户完成配置。[L85-L93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingdialog.cpp#L85-L93) 的 `data()` 每行显示 `TypeToString(type)`；双击列表（[L56-L60](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingdialog.cpp#L56-L60)）或点 Edit 按钮（[L61-L66](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingdialog.cpp#L61-L66)）都路由到同一个 `edit()`。

**对话框入口**。[deembedding.cpp:L12-L18](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.cpp#L12-L18)：`configure()` 槽创建 `DeembeddingDialog`，并遵守 u2-l1 讲过的无头规约——仅在 `AppWindow::showGUI()` 为真时 `show()`。

#### 4.2.4 代码实践

**实践目标**：跟踪"添加一个选项"在 GUI 上引发的完整联动链，理解 `optionAdded` 信号如何驱动 VNA 模式的菜单状态。

**操作步骤**：

1. 依次阅读以下四段代码，抄写调用链：
   - [deembeddingdialog.cpp:L95-L102](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingdialog.cpp#L95-L102)（`model.addOption` → `d->addOption` → `option->edit()`）
   - [deembedding.cpp:L219-L235](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.cpp#L219-L235)（`emit optionAdded`）
   - [vna.cpp:L227-L236](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L227-L236)（`optionAdded` → `EnableDeembedding(true)` 并启用两个菜单项）
   - [vna.cpp:L204-L225](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L204-L225)（菜单 "De-embedding" 的装配）
2. 若已按 u1-l3 编译了 GUI：启动后进入 VNA 模式，观察菜单 De-embedding → "De-embed VNA samples" 与 "De-embed traces..." 初始为禁用；打开 Setup... 添加任意一个选项后，这两项变为可用且前者自动勾选。

**需要观察的现象**：添加第一个选项的瞬间菜单启用与自动勾选是否同时发生；删除全部选项后（`allOptionsCleared`）是否回到禁用态。

**预期结果**：得到一条五步调用链 `DeembeddingDialog 添加 → Deembedding::addOption → emit optionAdded → VNA lambda → EnableDeembedding(true)`。若无法运行 GUI，步骤 2 标注「待本地验证」，仅完成步骤 1 的静态链路抄写即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `OptionModel::addOption` 必须把 `d->addOption(option)` 夹在 `beginInsertRows`/`endInsertRows` 之间？

**答案**：这是 QAbstractItemModel 的协议：任何会改变行数/列数的底层变更都要用 begin/end 对包裹，让_attached_ 视图（QListView）得以更新内部索引并重绘。若直接改数据不通知，视图要么不刷新、要么访问越界索引。

**练习 2**：交换两个选项的顺序（`swapOptions`）会改变什么、不改变什么？

**答案**：改变——数据流上的变换顺序（`Deembed` 中 `transformDatapoint` 的执行次序）、SCPI 地址（`updateSCPINames` 按新位置重编 1..n）、.setup 中 `toJSON` 的记录顺序。不改变——每个选项自身的参数与内部状态、它们的类型身份。

**练习 3**：`Deembedding` 构造函数把自己注册为 `SCPINode("DEEMBedding")`，而 [vna.cpp:L1664](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1664) 又 `SCPINode::add(&deembedding)`。最终的完整命令路径前缀是什么？

**答案**：`:VNA:DEEMBedding:...`。VNA 模式的 SCPI 节点是父节点，Deembedding 挂在其下，选项再按序号挂在 Deembedding 下，选项构造函数里 `addDoubleParameter` 等注册的参数位于最内层，例如 `:VNA:DEEMBedding:1:PORTEXTension:DELAY 100`。

### 4.3 与校准管线的衔接

#### 4.3.1 概念说明

这是本讲最关键的一块：去嵌入**站在管线的哪个位置**。答案由 [vna.cpp:L1058-L1069](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1058-L1069) 一段代码给出：平均 → **校准** → 写入 Trace 数据集 → （可选）**去嵌入** → 写入 Trace 去嵌入数据集。

三个推论：

1. **去嵌入的输入是校准后的数据**。选项的数学模型定义在 50Ω 校准参考面上——这正是 u9-l2 的误差模型所恢复的参考面，两级修正因此可以无缝衔接。
2. **校准数据与去嵌入数据并存**。Trace 内部有两份样本向量：`data`（校准后）与 `deembeddingData`（去嵌入后），由 `deembeddingActive` 标志决定 `getSample` 读哪一份。用户可以随时勾掉 "De-embed VNA samples" 回看未去嵌入的结果，原始数据不丢失。
3. **启用/禁用是模式级开关**。`deembedding_active` 布尔量在 VNA 类里；`optionAdded` 时自动置真，`allOptionsCleared` 时自动置假，也可由菜单手动切换（`EnableDeembedding`）。

本模块还要讲清**测量调度**：需要实测的选项（如端口延伸自动提取时延）如何借道 VNA 的扫描机制拿到一整条扫描的数据。

#### 4.3.2 核心流程

**数据上行管线**（每测量一个点执行一次，行号见 4.3.3）：

```
DeviceDriver 发出 VNAMeasurement
  ↓
average.process(m_avg)                          # u7-l4：平均（线性复数域）
  ↓
addStreamingData(Raw)                           # 流式出口 1
  ↓
cal.correctMeasurement(m_avg)                   # u9-l2：校准修正（就地）
  ↓
addStreamingData(Calibrated)                    # 流式出口 2
  ↓
traceModel.addVNAData(m_avg, type, false)       # 写入 Trace 的 data
  ↓ 若 deembedding_active
deembedding.Deembed(m_avg)                      # 本讲：逐选项 transformDatapoint
  ↓
addStreamingData(Deembedded)                    # 流式出口 3
  ↓
traceModel.addVNAData(m_avg, type, true)        # 写入 Trace 的 deembeddingData
```

**自动测量回路**（选项预约一次扫描）：

```
选项 edit() 中用户点击自动测量
  → emit DeembeddingOption::triggerMeasurement
  → Deembedding 设 measuringOption、弹出测量对话框（startMeasurementDialog）
     ├─ 路径 A：点 "Measure" → emit Deembedding::triggerMeasurement
     │    → VNA 记住原运行状态并 Run() 启动扫描
     │    → 扫描点流经 Deembed()，按 pointNum 收集进 measurements
     │    → 最后一点 → measurementCompleted() → measuringOption->measurementCompleted(m)
     │    → VNA 恢复原运行状态
     └─ 路径 B：从已有 Trace 选择 → assembleDatapoints 拼装 → 同样回调
```

#### 4.3.3 源码精读

**管线顺序的权威证据**。[vna.cpp:L1058-L1069](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1058-L1069)：

```cpp
cal.correctMeasurement(m_avg);

if(cal.getCaltype().type != Calibration::Type::None) {
    window->addStreamingData(m_avg, AppWindow::VNADataType::Calibrated, settings.zerospan);
}

traceModel.addVNAData(m_avg, type, false);
if(deembedding_active) {
    deembedding.Deembed(m_avg);
    window->addStreamingData(m_avg, AppWindow::VNADataType::Deembedded, settings.zerospan);
    traceModel.addVNAData(m_avg, type, true);
}
```

校准在前、去嵌入在后；u7-l4 讲过的三个流式通道（Raw/Calibrated/Deembedded）也在此处分岔——去嵌入是否启用决定了 Deembedded 通道有没有数据。

**Trace 的双数据集**。[trace.cpp:L170-L216](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L170-L216)：`addDeembeddingData` 与 u8-l1 见过的 `addData` 平行——同样维护 X 升序、同频点替换，只是写入的是 `deembeddingData` 向量，并额外记录 `deembedded_reference_impedance`（阻抗再归一化选项会改变参考阻抗，Smith 图读数需要知道新 Z0）。[L1603-L1618](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1603-L1618)：`getSample` 在 `deembeddingActive` 时优先读去嵌入数据集——开关一拨，所有图表、Marker 读到的就是另一份数据。[L793-L799](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L793-L799)：`getReferenceImpedance` 同样按标志返回对应 Z0。

**开关与数据清理**。[trace.cpp:L1459-L1490](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1459-L1490)：`setDeembeddingActive` 切换标志并发出 `deembeddingChanged` 与 `outputSamplesChanged`（通知图表重绘）；`clearDeembedding` 清空第二数据集并回到非激活态。[vna.cpp:L1868-L1882](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1868-L1882)：`EnableDeembedding` 是模式级实现——设置 `deembedding_active`、同步菜单勾选（`blockSignals` 防回环），再对所有 live Trace 调 `setDeembeddingActive(true)` 或 `clearDeembedding()`。

**测量预约的两条路径**。[deembedding.cpp:L34-L78](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.cpp#L34-L78)：`startMeasurementDialog` 创建进度对话框并内嵌 `SparamTraceSelector(tm, option->getAffectedPorts())`——`getAffectedPorts` 的第一个用途在此：限制只能选择覆盖受影响端口的 S 参数 Trace 组合。路径 A（实扫）：点 `bMeasure` 后置 `measuring = true` 并发出 `triggerMeasurement`；路径 B（复用数据）：点 OK 后用 `Trace::assembleDatapoints` 把选中 Trace 拼成数据点。两条路都汇合到 [L20-L32](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.cpp#L20-L32) 的 `measurementCompleted()`，它把 `measurements` 交给 `measuringOption->measurementCompleted(...)`。

**VNA 侧的启停与端口编排**。[vna.cpp:L237-L248](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L237-L248)：收到 `triggerMeasurement` 先记住当前是否在扫描（`wasRunningBeforeDeembeddingMeasurement`）再 `Run()`；`finishedMeasurement` 时按记忆恢复 Run 或 Stop——去嵌入测量不会顺带改变用户原本的运行状态。[vna.cpp:L1990-L1999](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1990-L1999)：`initializeDevice` 中若 `deembedding.isMeasuring()`，则 `excitedPorts` 改用 `deembedding.getAffectedPorts()`——`getAffectedPorts` 的第二个用途：测量期间只激励需要的端口（端口延伸只需单端口反射）。[vna.cpp:L1335](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1335)：`SetPoints` 时把点数告知 `setPointsInSweepForMeasurement`，`Deembed` 里判断"最后一点"（`pointNum == sweepPoints - 1`）就靠它。

**测量完成的数学：端口延伸如何自动定标**。[portextension.cpp:L141-L212](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp#L141-L212)：拿到整条扫描后，对每点取该端口反射系数（必要时先除掉校准件的理论值），逐点累加**解卷绕后的相位差**，平均得到每 Hz 的相位斜率，进而算出时延并除以 2（反射是往返、端口延伸要单向）；损耗则对 \(y_{\text{dB}}\) 与 \(x=\sqrt{f/f_{\max}}\) 做最小二乘线性回归，截距斜率映射出 DC 损耗与 \(\sqrt{f}\) 损耗系数。这是"接口的 `measurementCompleted` 槽能干什么"的完整示范。

**工作区持久化**。[vna.cpp:L900-L901](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L900-L901)：VNA 的 `toJSON` 存 `j["de-embedding"]`（选项列表）与 `j["de-embedding_enabled"]`（开关）；[L919-L924](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L919-L924)：`fromJSON` 恢复列表后按存档恢复开关（缺省为真）。对照 u2-l3：这属于工作区（Setup）级而非全局偏好级。

**手动去嵌入入口**。[vna.cpp:L218-L225](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L218-L225) 打开 `ManualDeembeddingDialog`；[manualdeembeddingdialog.cpp:L11-L34](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/manualdeembeddingdialog.cpp#L11-L34)：同样是按 `getAffectedPorts` 过滤的 Trace 选择器，确认前若所选 Trace 已有旧去嵌入数据会询问是否清除，最终调 `deemb->Deembed(traces)` 走 4.2.3 讲过的 Trace 重载——对**导入的静态 Trace** 应用同一套选项数学。

#### 4.3.4 代码实践

**实践目标**：在纸面上沿管线标注一次测点的旅程，并回答"改一个选项参数后，数据何时更新"。

**操作步骤**：

1. 打开 [vna.cpp:L1007-L1069](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1007-L1069)，为 `m_avg` 画一张状态变迁表：每个阶段它依次被谁修改（average / cal / deembedding 的各选项）、每阶段后被谁消费（streaming 通道、Trace 数据集）。
2. 回答两个问题（答案见下）：
   - a) 用户在选项 `edit()` 对话框里改了参数，正在滚动的扫描数据下一次经过 `NewDatapoint` 时会怎样？
   - b) 为什么 `traceModel.addVNAData(m_avg, type, false)`（写校准数据集）必须发生在 `deembedding.Deembed(m_avg)` **之前**？
3. （可选，需硬件或仿真环境）观察现象：去嵌入启用时用 `--no-gui` 加流式客户端同时订阅 Calibrated 与 Deembedded 两个通道，对比同一点的两份数据。

**需要观察的现象**：步骤 3 中两个通道的数据差异应恰好等于选项的变换（例如端口延伸的相位斜率）。

**预期结果**：
- a) 下一个点立即按新参数变换——`transformDatapoint` 每点都取当前参数，没有缓存；但已经写入 `deembeddingData` 的历史点不会自动重算，需要等下一圈扫描覆盖（或对静态 Trace 走手动去嵌入）。
- b) 因为 `Deembed` 是就地修改，若先变换再写校准数据集，`data` 里存的将是被去嵌入污染过的数据——关掉开关回看"仅校准"结果时就错了。先存后变换保证两份数据集互不串扰。
- 步骤 3 标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：去嵌入为什么不放在校准**之前**（先用选项修正原始数据，再做 SOLT 校准修正）？

**答案**：选项的数学模型定义在校准参考面上（典型为 50Ω 系统）。SOLT 校准输出的是"校准端面处的真实 S 参数"，只有它才可直接与端口延伸的传输线模型、2x-Thru 的夹具网络模型比较。若放在校准之前，选项面对的是掺有 12 项仪器误差的原始数据，模型无从建立。此外校准测量本身（u9-l2 的 `cal.addMeasurements`）也发生在 `correctMeasurement` 之前的数据流上，去嵌入绝不能污染校准的取数。

**练习 2**：`EnableDeembedding(false)`（取消菜单勾选）与 `clear()`（删除所有选项）对 Trace 数据的影响有何不同？

**答案**：`EnableDeembedding(false)` 只拨 `deembedding_active` 标志并对 live Trace `setDeembeddingActive(false)`——`deembeddingData` 仍在，重新勾选即可切回。`clear()` 逐个 `removeOption`，列表空后发 `allOptionsCleared`，VNA 侧会对 live Trace `clearDeembedding()`——第二数据集被清空，已去嵌入的数据丢失（静态 Trace 需重新走手动去嵌入）。

**练习 3**：去嵌入测量期间（`measuring == true`），若用户恰好更改了扫描点数，会发生什么？

**答案**：`VNA::SetPoints` 会调 `deembedding.setPointsInSweepForMeasurement(points)` 更新 `sweepPoints`（[vna.cpp:L1335](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1335)），而设置变更还会触发设备重配、数据点清零重来；`Deembed` 的收集逻辑以 `pointNum == 0` 重启、以 `pointNum == sweepPoints - 1` 收尾，因此测量会按新点数重新进行。这是"测量状态与扫描状态松耦合、靠点号对齐"设计的结果（另见 u7-l1 设置变更丢弃旧点的行为）。

## 5. 综合实践：实现 IdealAttenuator 去嵌入选项

把三个模块串起来：写一个最简选项"理想衰减器"——假设端口 2 前串了一个已知衰减量 \(a\)（dB）的理想衰减器，对 S21/S12 除以 \(k = 10^{-a/20}\) 把它从测量中移除。全程**不需要硬件**，用导入的 Touchstone 数据验证。

### 5.1 实践目标

- 亲手完成"新选项接入"的全部触点（4.1.4 的清单）。
- 体验插件式框架的红利：类型注册后，添加菜单、SCPI、持久化自动生效。
- 用手动去嵌入路径（4.3.3 最后一段）在导入数据上验证数学正确性。

### 5.2 操作步骤

以下代码均为**示例代码**（仓库中不存在，需自行创建）。

**第 1 步：新建 `VNA/Deembedding/idealattenuator.h`**

```cpp
#ifndef IDEALATTENUATOR_H
#define IDEALATTENUATOR_H

#include "deembeddingoption.h"

class IdealAttenuator : public DeembeddingOption
{
    Q_OBJECT
public:
    IdealAttenuator();
    std::set<unsigned int> getAffectedPorts() override;
    void transformDatapoint(DeviceDriver::VNAMeasurement& d) override;
    Type getType() override {return Type::IdealAttenuator;}
    void edit() override;
    nlohmann::json toJSON() override;
    void fromJSON(nlohmann::json j) override;
private:
    double attenuation; // 衰减量，单位 dB
};

#endif // IDEALATTENUATOR_H
```

**第 2 步：新建 `VNA/Deembedding/idealattenuator.cpp`**

```cpp
#include "idealattenuator.h"

#include <QInputDialog>
#include <cmath>

using namespace std;

IdealAttenuator::IdealAttenuator()
    : DeembeddingOption("IDEALATTenuator"),
      attenuation(10.0)   // 先给个非零默认值，方便观察效果
{
    addDoubleParameter("ATTenuation", attenuation);
}

std::set<unsigned int> IdealAttenuator::getAffectedPorts()
{
    return {1, 2};
}

void IdealAttenuator::transformDatapoint(DeviceDriver::VNAMeasurement &d)
{
    // 测量值 = DUT × k，k = 10^(-a/20)；去嵌入即除以 k
    auto factor = pow(10.0, -attenuation / 20.0);
    for(auto &m : d.measurements) {
        if(m.first == "S21" || m.first == "S12") {
            m.second /= factor;
        }
    }
}

void IdealAttenuator::edit()
{
    bool ok;
    double newAtt = QInputDialog::getDouble(nullptr, "Ideal Attenuator",
                       "Attenuation to remove (dB):", attenuation,
                       -100.0, 100.0, 3, &ok);
    if(ok) {
        attenuation = newAtt;
    }
}

nlohmann::json IdealAttenuator::toJSON()
{
    nlohmann::json j;
    j["attenuation"] = attenuation;
    return j;
}

void IdealAttenuator::fromJSON(nlohmann::json j)
{
    attenuation = j.value("attenuation", 0.0);
}
```

对照 4.1.3 的 PortExtension：构造函数传 SCPI 名、用 `addDoubleParameter` 挂远程参数、`transformDatapoint` 做除法——结构完全同构，只是数学更简单。

**第 3 步：登记到 .pro**。在 [LibreVNA-GUI.pro](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro) 的 HEADERS 段（L147-154，按字母序插到 `impedancerenormalization.h` 之后）加：

```
VNA/Deembedding/idealattenuator.h \
```

SOURCES 段（L305-312）同样加：

```
VNA/Deembedding/idealattenuator.cpp \
```

（u1-l3 讲过：.pro 是工程的单一事实来源，不登记的文件不参与编译。）

**第 4 步：注册类型**。在 [deembeddingoption.h:L16-L23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingoption.h#L16-L23) 的枚举中、注释行之前加 `IdealAttenuator,`；在 [deembeddingoption.cpp:L8-L22](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingoption.cpp#L8-L22) 的 `create()` 加：

```cpp
case Type::IdealAttenuator:
    return new IdealAttenuator();
```

并在 [deembeddingoption.cpp:L24-L38](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembeddingoption.cpp#L24-L38) 的 `TypeToString()` 加：

```cpp
case Type::IdealAttenuator:
    return "Ideal Attenuator";
```

同时在 deembeddingoption.cpp 顶部补 `#include "idealattenuator.h"`。**到此为止**——对话框菜单、SCPI `NEW`、JSON 加载都不需要再改（回到 4.1.4 清单核对）。

**第 5 步：编译**（承接 u1-l3 的构建方式）：

```bash
cd Software/PC_Application/LibreVNA-GUI
qmake6 && make -j$(nproc)
```

**第 6 步：无硬件验证**：

1. 启动 GUI（无需连接设备），按 u1-l3 的方式导入 `Documentation/Measurements` 下的示例测量（或任意 S2P Touchstone），得到 S11/S21/S12/S22 四条 Trace。
2. 菜单 De-embedding → Setup... → 点 Add 旁的小箭头 → 应看到新菜单项 "Ideal Attenuator"（这是 4.2.3 枚举遍历菜单的直接验证），点击后立即弹出 QInputDialog（OptionModel 的自动 `edit()`）。
3. 菜单 De-embedding → "De-embed traces..."，选中导入的 S 参数 Trace 组合，确认。
4. 在 S21 的 XY 图（dB 轴）上放一个 Marker 读数。

### 5.3 需要观察的现象

- 添加选项后，"De-embed VNA samples" 与 "De-embed traces..." 菜单自动变为可用（4.2 的 `optionAdded` 联动）。
- 执行手动去嵌入后，S21 与 S12 的 dB 曲线**整体上移 attenuation dB**（默认 10 dB），S11/S22 纹丝不动——因为 `transformDatapoint` 只碰了这两个键。
- 在 `edit()` 中把衰减量改为 20 dB 再去嵌入一次，曲线上移量翻倍；改为 0 dB 则与原始曲线重合（除以 1）。
- 保存 .setup 后用文本编辑器打开，能在 `de-embedding` 数组里看到 `{"operation": "Ideal Attenuator", "settings": {"attenuation": ...}}`——`TypeToString` 的返回值就是持久化身份。

### 5.4 预期结果

数学核对：设导入的 S21 在某频点为 −20.5 dB，衰减量 10 dB 时去嵌入后应读得 −10.5 dB：

\[ |S_{21}^{\text{DUT}}|_{\text{dB}} = |S_{21}^{\text{meas}}|_{\text{dB}} + a = -20.5 + 10 = -10.5 \text{ dB} \]

相位不变（\(k\) 为正实数，无相角）。若观察结果不符，优先检查 `transformDatapoint` 中除法的方向（是除以 \(10^{-a/20}\) 而不是乘以它）。SCPI 路径（`:VNA:DEEMBedding:NEW "Ideal_Attenuator"` 等）的验证需要开启 TCP 服务器，可留到 u10-l2 一起做，此处标注「待本地验证」。

### 5.5 收尾思考

把这个选项与 PortExtension 对比：两者的差别只在 `transformDatapoint` 的数学与 `edit()`/`measurementCompleted()` 的复杂度。框架把"注册身份、列表管理、界面入口、测量调度、持久化、SCPI"全部承担掉了——这正是插件式分层的价值：**新增一种物理修正只需要新增一个数学类**。下一讲（u9-l5）将逐一精读四个内置选项的数学。

## 6. 本讲小结

- `DeembeddingOption` 是"一种夹具修正算法"的抽象：三个纯虚函数（`getAffectedPorts` / `transformDatapoint` / `getType`）是契约，`edit()` 与 `measurementCompleted()` 是可选能力；`transformDatapoint` 就地修改测量点，使多选项天然按列表顺序接力。
- 类型系统由 Type 枚举 + `TypeToString` 字符串双轨构成：字符串同时是 UI 菜单名、JSON `operation` 身份和 SCPI `NEW` 参数；新增类型只需枚举、工厂、字符串、.pro 四处改动，对话框菜单与持久化加载靠枚举遍历自动生效。
- `Deembedding` 容器管理选项列表（增删换序、SCPI 按 1..n 重编号、JSON 按序持久化），`DeembeddingDialog`/`OptionModel` 是它的 UI 投影；新选项添加后立即弹出 `edit()`。
- 管线位置是本讲的核心事实：**平均 → 校准 → 写入 `data` → 去嵌入 → 写入 `deembeddingData`**。去嵌入学的是校准后数据；Trace 双数据集并存，`deembeddingActive` 标志决定读哪份，开关切换不丢数据。
- 需要实测的选项经 `triggerMeasurement` 信号预约扫描：VNA 记住运行状态并启动扫描，`Deembed` 按 `pointNum` 收集整条扫描后回调 `measurementCompleted`，测量期间激励端口改用 `getAffectedPorts`。
- 手动去嵌入（`Deembed(map<QString,Trace*>)` 重载 + `ManualDeembeddingDialog`）把同一套选项数学应用到导入的静态 Trace 上，是无硬件开发与验证选项的主要路径。

## 7. 下一步学习建议

本讲搞定了框架与接入方法，下一讲 **u9-l5（去嵌入实战：端口延伸、阻抗与 2x-Thru）** 将深入四个内置选项的数学：portextension 的相位/损耗模型（本讲已见其核心 20 行）、impedancerenormalization 的参考阻抗变换矩阵、matchingnetwork 的集总元件网络、twothru 的夹具网络求解。建议带着两个问题去读：

1. 阻抗再归一化为什么必须改变 Trace 的 `deembedded_reference_impedance`（提示：回忆 4.3.3 中 `getReferenceImpedance` 的双轨返回）？
2. 2x-Thru 需要哪些测量输入，它如何把夹具网络从总测量中分离？

另外一个有趣的对照方向：把本讲的 `DeembeddingOption` 插件体系与 u8-l5 的 `TraceMath` 运算链比较——两者都是"抽象基类 + 工厂 + 类型枚举 + 自动注册的 UI"，但 TraceMath 是**有向图**且作用于 Trace 显示数据，DeembeddingOption 是**有序列表**且作用于测量管线上的校准后数据。理解这对"同构不同位"的设计，比单独理解任何一个都更有价值。
