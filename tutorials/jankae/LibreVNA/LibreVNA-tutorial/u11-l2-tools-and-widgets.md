# 工具箱：S 参数数学、E 系列、阻抗匹配与自定义控件

## 1. 本讲目标

学完本讲，你应该能够：

1. 熟练使用 `Tools/parameters.h` 提供的五个参数类（`Sparam`/`Zparam`/`Yparam`/`ABCDparam`/`Tparam`）完成参数域互换、矩阵级联与求逆，并理解它们在校准（单元 9）和去嵌入（u9-l4/l5）中被消费的方式。
2. 读懂三个「工具箱」应用的算法：`ESeries` 标准值逼近、`ImpedanceMatchDialog` 的双元件 L 网络解析解、`MixedModeConversion` 的 16 条差模/共模公式。
3. 把 `SIUnitEdit` + `Unit` 这对「SI 单位输入」组件复用到自己的界面代码里，并说清它的事件过滤与解析流程。
4. 说清 Eigen 库在 LibreVNA 数值代码中的角色：哪里用它的通用矩阵运算，哪里故意绕开它。

本讲是「阅读型」讲义为主：这三个模块几乎都不依赖硬件，你可以在没有设备的情况下完成全部实践。

## 2. 前置知识

### 2.1 S 参数的矩阵观

u8-l1 讲过 Trace 里存的「一个频点一个复数」。本讲把这些复数放回它们本来的形态——矩阵。对 N 端口网络，每个频点有一个 N×N 的 S 矩阵：\( S_{ij} \) 是「端口 j 激励、端口 i 响应」时的反射/传输系数。LibreVNA 单机是 2 端口（2×2），配合 CompoundDriver（u3-l3）或导入 4 端口 Touchstone（u8-l4）可以得到 4×4。

### 2.2 为什么要换「域」

S 参数对测量友好，但对推理未必。同一个网络在不同参数域下有不同的好性质：

| 参数域 | 好性质 | LibreVNA 中的用途 |
|---|---|---|
| S | 直接可测，与反射/传输对应 | 测量结果的通用语言 |
| Z（阻抗） | 串联直接相加；并联也简单 | 阻抗再归一化去嵌入（u9-l5） |
| Y（导纳） | 并联直接相加 | 匹配网络里的并联元件 |
| ABCD（传输） | 二端口级联 = 矩阵乘法 | 匹配网络 / 2x-Thru 去嵌入 |
| T（波传输） | 二端口级联 = 矩阵乘法 | TRL 校准、2x-Thru 误差盒 |

本讲的主角 `parameters.h` 就是这五个域之间的「换乘站」。

### 2.3 Eigen 三分钟入门

Eigen 是仅头文件的 C++ 线性代数库。LibreVNA 只用了它最基础的部分：

- `Eigen::MatrixXcd`：动态大小的复数双精度矩阵（X = 动态维度，cd = complex double）。
- `A * B`、`A + B`：矩阵乘/加；`A.inverse()`：通用求逆；`Eigen::MatrixXcd::Identity(n,n)`：单位阵。

源码里以源码形式内置（无需安装），在 GUI 的 .pro 工程里直接编译。

### 2.4 SI 词头与 E 系列

- SI 词头：`p`(1e-12)、`n`(1e-9)、`u`(1e-6)、`m`(1e-3)、`k`(1e3)、`M`(1e6)、`G`(1e9)……仪器界面里输入 `1.5G` 比 `1500000000` 友好得多。
- E 系列：电阻/电容/电感的工业化标准值序列。E6 每个十倍程 6 个值（1.0/1.5/2.2/3.3/4.7/6.8），E96 有 96 个。理论计算出的「17.3 nH」买不到，只能贴到 16 或 18 nH——阻抗匹配工具必须回答「贴完之后匹配还剩多少」。

### 2.5 一个容易踩的复数坑

`std::norm(z)` 返回的是 \( |z|^2 \)（模的平方），不是模长。`impedancematchdialog.cpp` 里大量使用 `norm(Z)`，读代码时记住这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `Software/PC_Application/LibreVNA-GUI/Tools/parameters.h/.cpp` | 参数数学库：五域互换、级联、求逆、开方（本讲主角） |
| `Software/PC_Application/LibreVNA-GUI/Tools/eseries.h/.cpp` | 把任意值贴到 E6~E96 标准值 |
| `Software/PC_Application/LibreVNA-GUI/Tools/impedancematchdialog.cpp` | 双元件 L 网络阻抗匹配计算器 |
| `Software/PC_Application/LibreVNA-GUI/Tools/mixedmodeconversion.cpp` | 4 端口单端 → 差模/共模混合模式转换 |
| `Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.h/.cpp` | SI 单位输入框（本讲的复用样板） |
| `Software/PC_Application/LibreVNA-GUI/unit.cpp` | 纯函数层：`Unit::ToString/FromString/SIPrefixToFactor` |

配套但不展开精读的「消费者」：`VNA/vna.cpp`（Tools 菜单挂载点）、`VNA/Deembedding/*.cpp` 与 `Calibration/*.cpp`（parameters 的下游用户）、`Util/util.h`（dB 换算）。

三个工具入口都在 VNA 窗口的 Tools 菜单下（见 [VNA/vna.cpp:250-257](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L250-L257)）：`Tools → Impedance Matching` 与 `Tools → Mixed Mode Conversion`。

## 4. 核心概念与源码讲解

### 4.1 parameters 数学库：一个 Eigen 矩阵，五种视图

#### 4.1.1 概念说明

`parameters.h` 的设计思路非常克制：**数据只有一个**（复数矩阵），**类有五个**（S/Z/Y/ABCD/T），每个类只是同一块数据的「域标签」。域之间的转换全部写成「转换构造函数」——你想把 S 参数换到 ABCD 域，就构造一个 `ABCDparam(S, Z0)`，C++ 的类型系统保证你不会把一个 Z 矩阵当 S 矩阵用。

为什么值得专门写一个库？因为校准（u9-l2）与去嵌入（u9-l4/l5）的数学本质都是「在网络级联中剥离已知的一段」：级联在 ABCD/T 域是矩阵乘法，剥离是求逆，而 S↔其他域的换算是前提。把这些公式集中在一处、配一套单元测试（u11-l1 的 `parametertests.cpp`），上层模块就不必各自抄公式。

#### 4.1.2 核心流程

五域换乘地图（全部为复数运算）：

- S → Z（N 端口通用）：

\[ Z = \sqrt{z}\,(I+S)\,(I-S)^{-1}\,\sqrt{z} \]

  其中 \(\sqrt{z}\) 是以各端口特性阻抗平方根为对角元的对角阵，\(I\) 是单位阵。
- Z → S（同一公式的反向）：

\[ S = \left(\sqrt{y}\,Z\,\sqrt{y}-I\right)\left(\sqrt{y}\,Z\,\sqrt{y}+I\right)^{-1},\quad \sqrt{y}=\sqrt{z}^{-1} \]

- S ↔ ABCD（仅 2 端口，支持两端不同特性阻抗 `Z01`/`Z02`）：闭式公式互转。
- S → T（仅 2 端口）：波传输参数。
- Z ↔ Y：`Y = Z⁻¹`（导纳矩阵 = 阻抗矩阵求逆）。
- ABCD 与 T 的级联都是 `data * r.data`（矩阵乘法）；两者都手写了 2×2 求逆 `inverse()` 与矩阵开方 `root()`。

| 类 | 独有能力 |
|---|---|
| `Sparam` | `swapPorts`（行列互换）、`reduceTo`（取子矩阵）、`operator+`、`operator*(Type)` |
| `Zparam` / `Yparam` | 互为求逆 |
| `ABCDparam` | `operator*` 级联、`inverse`、`root`、双端不同 Z0 |
| `Tparam` | `operator+`、级联、`inverse`、`root` |

#### 4.1.3 源码精读

**基类：一块矩阵 + 1 基下标**。文献里 S 参数都是 1 基下标，Eigen 是 0 基，所以基类把偏移封装掉（[Tools/parameters.h:10-29](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.h#L10-L29)）：

```cpp
using Type = std::complex<double>;

class Parameters : public Savable {
public:
    ...
    Eigen::MatrixXcd data;
    unsigned int ports() const { return data.cols();}
    // Access to elements is usually off-by-one (mostly 1-based indexing in literature but Eigen uses 0-based indexing)
    Type get(unsigned int row, unsigned int col) const {return data(row-1, col-1);}
    void set(unsigned int row, unsigned int col, Type t) { data(row-1, col-1) = t;}
```

这段代码做了三件事：定义全库统一的复数类型别名 `Type`；把唯一的存储 `data` 设为 public（让上层可以直接用 Eigen 表达式运算，代价是破坏封装——这是一个明确的取舍）；用 `get/set` 把「1 基」翻译成「0 基」。注意 `Parameters` 还继承了 `Savable`（u2-l3），所以参数矩阵可以直接进 JSON 持久化，键名形如 `m11_real`/`m11_imag`（实现在 [Tools/parameters.cpp:125-167](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L125-L167)）。

**S ↔ Z：Eigen 通用公式**。[Tools/parameters.cpp:186-208](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L186-L208) 是 N 端口通用版本的 S→Z：

```cpp
Zparam::Zparam(const Sparam &S, std::vector<Type> Z0n)
{
    if(S.ports() != Z0n.size()) {
        throw std::runtime_error("number of supplied characteristic impedances does not match number of ports");
    }
    // create identity matrix
    auto ident = Eigen::MatrixXcd::Identity(S.ports(), S.ports());
    // create sqrt(z) matrix
    Eigen::MatrixXcd sqrtz = Eigen::MatrixXcd::Zero(S.ports(), S.ports());
    // fill with characteristic impedance
    for(unsigned int i=0;i<S.ports();i++) {
        sqrtz(i, i) = sqrt(Z0n[i]);
    }
    // apply formula
    data = sqrtz*(ident+S.data)*(ident-S.data).inverse()*sqrtz;
}
```

四个要点：

1. 端口数与阻抗表长度不匹配时抛 `std::runtime_error`——调用方（如 `impedancerenormalization.cpp`）用 try/catch 兜住，这是本库唯一的错误处理方式。
2. 每端口可以有**不同的**特性阻抗（`std::vector<Type>`，且类型是复数——支持复阻抗参考面），这正是阻抗再归一化去嵌入需要的通用性。
3. `(ident-S.data).inverse()` 是 Eigen 的通用 LU 求逆，N 端口一律可用。
4. 反向的 `Sparam(Zparam)` 在 [Tools/parameters.cpp:27-50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L27-L50)，公式对偶（\(\sqrt{z}\) 换成 \(\sqrt{y}\)，乘加变加减除）。

**Eigen 的角色：该用就用，该绕就绕**。最能体现「刻意使用 Eigen」与「刻意绕开 Eigen」并存的是 2×2 求逆的手写实现（[Tools/parameters.h:85-94](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.h#L85-L94)）：

```cpp
ABCDparam inverse() {
    ABCDparam i;
    // by hand, this is faster because the Eigen matrix is using dynamic size
    Type det = data(0,0)*data(1,1) - data(0,1)*data(1,0);
    i.data(0,0) = data(1,1) / det;
    ...
}
```

注释说得很直白：`MatrixXcd` 是动态尺寸类型，Eigen 的通用求逆要为「任意大小」付出抽象成本；而 2×2 闭式求逆只有一次除法分支。同一个文件里，N 端口的 S↔Z 用 `inverse()`，固定 2×2 的 ABCD/T 手写——**按问题规模选工具**，这就是 Eigen 在本项目的角色。同理，级联 `ABCDparam::operator*` 直接用 `data * r.data`（[Tools/parameters.h:79-84](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.h#L79-L84)），因为矩阵乘法本身就是 ABCD 级联的定义。

**多端口裁剪：`reduceTo` 与 `swapPorts`**。[Tools/parameters.cpp:64-73](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L64-L73)：

```cpp
// Example: 4 port S parameters as an input but we want the 2 port data from the original ports 1 and 3
// Call: S.reduceTo(1, 3)
Sparam Sparam::reduceTo(std::vector<unsigned int> ports) const
{
    auto ret = Sparam(ports.size());
    for(unsigned int from=0;from<ports.size();from++) {
        for(unsigned int to=0;to<ports.size();to++) {
            ret.data(to, from) = get(ports[to], ports[from]);
        }
    }
    return ret;
}
```

`swapPorts`（[Tools/parameters.cpp:58-62](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L58-L62)）把一行一列同时交换。这两个工具函数是多端口世界的日常：CompoundDriver 聚合出的 4 端口数据、或导入的 4 端口 Touchstone，在喂给只懂 2 端口的算法前都要先裁剪——真实调用点如 [VNA/Deembedding/twothru.cpp:32](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L32)、[Calibration/calibrationmeasurement.cpp:432](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp#L432)（`m.toSparam().reduceTo({port1, port2})`），TRL 校准里的端口对调见 [Calibration/calibration.cpp:836](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L836)。

**下游用户一览**（印证「为什么要有这个库」）：

- 阻抗再归一化去嵌入：`auto Z = Zparam(S, p.Z0);`（[VNA/Deembedding/impedancerenormalization.cpp:41](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/impedancerenormalization.cpp#L41)）——u9-l5 讲过的 S→Z→S′ 换域。
- 匹配网络去嵌入：`Sparam(m.forward * ABCDparam(S, p.Z0), p.Z0)`（[VNA/Deembedding/matchingnetwork.cpp:138](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/matchingnetwork.cpp#L138)）——先转 ABCD 级联再转回 S。
- 2x-Thru：误差盒开方与 T 参数级联（[VNA/Deembedding/twothru.cpp:642-675](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L642-L675)）。
- TRL 校准：`Tparam(Sthrough)`、`Tparam(Sline)`（[Calibration/calibration.cpp:1010-1011](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L1010-L1011)）。

另外两处值得一看的细节：`Parameters(int num_ports)` 用 `Eigen::MatrixXd::Zero`（实数矩阵）初始化再隐式转换进 `MatrixXcd data`（[Tools/parameters.cpp:120-123](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L120-L123)），效果是「维度正确、元素全 0 的复数阵」；`Tparam(const Sparam&)` 抛出的错误信息写的是 "Can only create **ABCD** parameter..."（[Tools/parameters.cpp:88-92](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L88-L92)）——从 ABCD 版本复制粘贴时忘了改词，无伤大雅，但说明错误信息也是会撒谎的。

#### 4.1.4 代码实践

**实践目标**：不依赖硬件，用一次「可手算」的 S→Z 换算验证你理解了构造函数与 1 基下标。

**操作步骤**：

1. 打开 [Software/PC_Application/LibreVNA-Test/parametertests.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp)，找到现成的 `S2Z_1P` 测试（第 65 行起），它已经在做本实践要做的事。
2. 在该测试类中新增一个私有槽（例如 `S2Z_manual`），写 5 行：

```cpp
// 示例代码：新增到 ParameterTests 类中的测试槽
void ParameterTests::S2Z_manual()
{
    auto S = Sparam(std::complex<double>(0.2, 0.0)); // 单端口，S11 = 0.2（实数）
    auto Z = Zparam(S, 50.0);                        // 50 欧姆系统
    QCOMPARE(Z.get(1,1).real(), 75.0);               // 手算期望值，见下
}
```

3. 在 `parametertests.h` 中声明该槽，重新编译并运行 `LibreVNA-Test`（构建方式见 u11-l1；新文件需登记进测试工程的 .pro，本实践只是加成员函数，无需改 .pro）。

**需要观察的现象**：测试通过，`Z.get(1,1)` 恰为 75.0。

**预期结果**：单端口反射系数与阻抗的关系是 \( Z = Z_0\,\dfrac{1+\Gamma}{1-\Gamma} \)，代入 \(\Gamma=0.2\)、\(Z_0=50\,\Omega\) 得 \( Z = 50 \times 1.2/0.8 = 75\,\Omega \)。这正是公式 \( Z=\sqrt{z}(I+S)(I-S)^{-1}\sqrt{z} \) 在 N=1 时的退化形式，也与 `S2Z_1P` 中已有的断言 `Z11 = (1.0+S11)/(1.0-S11)*Z0` 一致。若断言失败，先检查你是否误用了 0 基下标（`get(0,0)` 会怎样？见练习 3）。

#### 4.1.5 小练习与答案

**练习 1**：`Zparam(S, Z0)` 必须传特性阻抗，而 `Tparam(S)` 不用。为什么？

**答案**：Z 参数有阻抗量纲，是从「归一化的波」回到「绝对电压/电流」的换算，必须知道参考阻抗；T 参数是同一条传输线上归一化波之间的变换，归一化已经约掉，不需要额外信息。同理 `ABCDparam(S, Z01, Z02)` 也必须传（且允许两端不同）。

**练习 2**：`Sparam(0.0, 1.0, 1.0, 0.0)`（twothru.cpp:642 中用到）代表什么网络？

**答案**：理想直通：S11=S22=0（完全匹配、无反射），S21=S12=1（无损耗全传输）。它的 ABCD 形式是单位阵的近亲，2x-Thru 算法用它作初值。

**练习 3**：对 2 端口 `Sparam` 调 `get(0,0)` 或 `get(3,3)` 会发生什么？

**答案**：`get(0,0)` 实际访问 `data(-1,-1)`，`get(3,3)` 访问 `data(2,2)`——两者都是 Eigen 动态矩阵的越界访问，属于未定义行为，不会抛异常（`operator[]`/`()` 不做边界检查）。库只在「端口数 vs 阻抗数」这类它看得见的地方抛异常，下标合规要靠调用者自律。

---

### 4.2 实用工具：E 系列、阻抗匹配与混合模式转换

#### 4.2.1 概念说明

Tools 目录里除了数学库，还有三个「面向使用者的小工具」，全部挂在 VNA 窗口的 Tools 菜单：

1. **ESeries**（无界面，纯函数）：把理论值贴到可购买的标准值。它是阻抗匹配对话框的附属品——没有它，算出的 17.3 nH 就只是纸上谈兵。
2. **ImpedanceMatchDialog**：给定一个（通常来自 Marker 读数的）阻抗，设计一个双元件 L 匹配网络把它匹配到 50 Ω，并回答「用了 E 系列真实元件后匹配还剩多少 dB」。
3. **MixedModeConversion**：把 4 端口单端 S 参数转换成差模/共模（mixed mode）S 参数，是差分电路测量的必备翻译器。

三者的关系是一条加工链：**测量 → 换视角（mixed mode）→ 得到阻抗 → 设计匹配网络 → 贴现实元件（E 系列）**。

#### 4.2.2 核心流程

**ESeries 算法**（三步）：

1. 归一化：`shift10 = floor(log10(value))`，把值缩进 \([1, 10)\)；
2. 表扫描：从下标 1 起找第一个大于 value 的表项，得到下界 `lower` 与上界 `higher`；
3. 按类型取值（`Lower`/`Higher`/`BestMatch` 取更近者），乘回 \(10^{shift10}\)。

**阻抗匹配算法**（`calculateMatch` 的骨架）：

```
输入 Z（串联形式直接读，并联形式先算 Z = Zr·Zj/(Zr+Zj)）
├─ 若 Re(Z) > Z0：并联元件导纳 B 起头
│    B = sqrt(Re(Z)/Z0)·sqrt(|Z|² − Z0·Re(Z))（串联 C 时取负）
│    B = (B + Im(Z)) / |Z|²
│    X = 1/B + Im(Z)·Z0/Re(Z) − Z0/(B·Re(Z))
├─ 否则：串联元件电抗 X 起头
│    B = sqrt((Z0−Re(Z))/Re(Z))/Z0，X = sqrt(Re(Z)·(Z0−Re(Z)))（串联 C 时双双取负）
│    X = X − Im(Z)
├─ X、B 换算成 L/C：X>0 是电感 L=X/(2πf)；B>0 是电容 C=B/(2πf)；
│   符号组合出 L+C / 双 C / 双 L 三种拓扑
├─ L、C 贴 E 系列（可选档位）
└─ 回算：用贴过的真实元件重组网络，算 Zmatched 与残余反射 |Γ|，显示回波损耗
```

其解析解来自一份 KU 大学讲义（源码注释中给出了出处链接，见 [Tools/impedancematchdialog.cpp:98](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/impedancematchdialog.cpp#L98)）。

**混合模式转换**：把单端 4 端口按 (1,3)、(2,4) 配成两个「模式口」，每个模式口有差模（D）与共模（C）两种激励方式，组合出 4×4=16 个混合模式 S 参数。核心是叠加系数 0.5，例如：

\[ S_{DD11} = \tfrac{1}{2}\left(S_{11}-S_{13}-S_{31}+S_{33}\right),\qquad S_{DD21} = \tfrac{1}{2}\left(S_{21}-S_{23}-S_{41}+S_{43}\right) \]

\[ S_{CC21} = \tfrac{1}{2}\left(S_{21}+S_{23}+S_{41}+S_{43}\right),\qquad S_{DC21} = \tfrac{1}{2}\left(S_{21}+S_{23}-S_{41}-S_{43}\right) \]

模式口的等效阻抗不同：差模口 \( Z_{0,\text{diff}} = 2Z_0 \)（两口等幅反相，电压相加电流相消），共模口 \( Z_{0,\text{comm}} = Z_0/2 \)（两口并联）。这些公式也可以写成矩阵形式 \( S_{mm} = M S M^{\mathsf T} \)，其中 \(M\) 是正交的模式变换阵（见 4.2.4 实践）。

#### 4.2.3 源码精读

**ESeries：数据表 + 线性扫描**。五个系列各是一张静态表（[Tools/eseries.cpp:6-20](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/eseries.cpp#L6-L20)），核心函数 [Tools/eseries.cpp:22-61](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/eseries.cpp#L22-L61)：

```cpp
// bring value into [1.0, 10.0) interval
int shift10 = floor(log10(value));
value *= pow(10.0, -shift10);
...
unsigned int index = 1;
while(index < 96 && series[index] <= value) {
    index++;
}
auto lower = series[index - 1];
double higher = 10.0;
if(index < series.size()) {
    higher = series[index];
}
```

注意两处细节：`higher` 的默认值是 10.0——当 value 大于表内最大项（比如 E96 的 9.76）时，「更高的标准值」就是下一个十倍程的第一个值，此时 `index == series.size()`，越界检查保护了这里；`Ideal` 档与非法输入（value ≤ 0）直接原样返回（第 24-27 行），这就是阻抗匹配对话框里「理想元件」选项的实现。但循环条件 `index < 96` 只对 E96（96 项）是安全上界——对 E6/E12 等更短的表，若归一化后的 value 超过表内最大项（例如把 8.0 贴到 E6，E6 最大是 6.8），`series[index]` 会以 `index == series.size()` 进入循环条件，这是 `std::vector::operator[]` 的越界读（未定义行为）。按 C++ 语言语义这是确定的缺陷，实际运行表现取决于堆内存布局，**待本地验证**；修复方式是把 96 换成 `series.size()`。读代码时保持这种「上界与容器长度不匹配」的敏感度，正是 u11-l1 所说「期望值须来自被测代码之外」的另一种练习。

**阻抗匹配：从 Marker 到元件值的一条龙**。对话框构造函数把三个输入框设成 SI 单位编辑框并连上重算槽（[Tools/impedancematchdialog.cpp:19-40](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/impedancematchdialog.cpp#L19-L40)）；数据来源支持「从 Marker 读」——只有反射类 Trace 的 Marker 才可选（第 43-54 行的 `isReflection()` 过滤），读到 Γ 后换算成阻抗并取该 Trace 的参考阻抗（第 73-77 行）：

```cpp
Z0 = m->getTrace()->getReferenceImpedance();
auto reflection = Z0 * (1.0 + data) / (1.0 - data);
ui->zReal->setValue(reflection.real());
ui->zImag->setValue(reflection.imag());
```

这就是 \( Z = Z_0(1+\Gamma)/(1-\Gamma) \) 的逐字实现——u8-l3 的 Marker 只给复数 Γ，物理意义要靠这一行补全。两个解析解分支在 [Tools/impedancematchdialog.cpp:100-116](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/impedancematchdialog.cpp#L100-L116)；X/B 到 L/C 的换算与三态拓扑判定（一个 L 一个 C / 双 C / 双 L）在 [Tools/impedancematchdialog.cpp:121-138](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/impedancematchdialog.cpp#L121-L138)：

```cpp
if(X >= 0) {
    L = X/(2*M_PI*freq);
    if(B > 0) {
        C = B/(2*M_PI*freq);
    } else {
        ... // 双电感拓扑
    }
} else {
    C = -1/(X*2*M_PI*freq);
    ...
}
```

物理直觉：正电抗是电感（\(X=\omega L\)），正电纳是电容（\(B=\omega C\)）；符号的四种组合恰好对应四种 L 网络拓扑。随后贴 E 系列（第 169-172 行），**再用贴过的元件值重组网络回算**真实匹配效果（第 215-249 行）：

```cpp
Zmatched = Z*Zp/(Z+Zp) + Zs;              // 或并联在前的对偶式，取决于拓扑
ui->mReal->setValue(Zmatched.real());
double reflection = abs((Zmatched-Z0)/(Zmatched+Z0));
auto loss = Util::SparamTodB(reflection);  // 20*log10(|Γ|)，回波损耗
```

\( \Gamma_{matched} = \dfrac{Z_{matched}-Z_0}{Z_{matched}+Z_0} \)（[Util/util.h:30-35](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.h#L30-L35) 的 `SparamTodB` 只是 \(20\lg|\cdot|\)）。整个函数包在 try/catch 里，频率为 0 等非法中间结果统一显示 NaN（第 273-280 行）——数值工具对「用户还没输完」的输入必须宽容。

**混合模式转换：公式即字符串**。对话框本体不自己算数学，而是为每个目标 Trace 设置一条**字符串公式**，交给 u8-l5 的 Trace 数学引擎执行（[Tools/mixedmodeconversion.cpp:119-137](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/mixedmodeconversion.cpp#L119-L137)）：

```cpp
std::vector<Destination> destinations = {
    Destination(sources, prefix+"SDD11", "0.5*(S11-S13-S31+S33)", 2*ui->selector->getReferenceImpedance()),
    Destination(sources, prefix+"SDD12", "0.5*(S12-S14-S32+S34)", 2*ui->selector->getReferenceImpedance()),
    ...
    Destination(sources, prefix+"SCC11", "0.5*(S11+S13+S31+S33)", 0.5*ui->selector->getReferenceImpedance()),
    ...
};
```

三个观察：

1. **配对约定**：差分对是端口 (1,3) 与 (2,4)——公式里 SDD11 只出现 S11/S13/S31/S33 印证了这一点。测量时必须把差分线的两端按这个约定接。
2. **参考阻抗跟响应模式走**：名字第二个字母（响应模式）是 D 就设 \(2Z_0\)，是 C 就设 \(Z_0/2\)（`setReferenceImpedance`，见第 76 行）。这决定了 Smith 图等 Y 轴解释的正确性。
3. **库缺失**：这 16 条公式没有像 `Zparam` 那样进入 parameters 数学库，而是以字符串形式内联在 GUI 代码里。`Destination` 辅助类（第 68-114 行）从公式字符串里逐个捞出 `S11`...`S44` 字样再绑定源 Trace（`addMathSource`），名字最后两位相同则标记为反射 Trace（第 78 行）。产物经 `tracesCreated` 信号交给 `TraceImportDialog` 入库（[VNA/vna.cpp:1123-1135](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1123-L1135)），从此它们就是普通的数学 Trace，能绘图、加 Marker、导出。

#### 4.2.4 代码实践

**实践目标**：不依赖硬件，在 GUI 里完成一次可手算验证的阻抗匹配设计。

**操作步骤**：

1. 启动 LibreVNA-GUI（无需连接设备），进入 VNA 模式，菜单 `Tools → Impedance Matching`。
2. 数据源保持第一项（手工输入；此时 `Z0 = 50.0`，见 [Tools/impedancematchdialog.cpp:79](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/impedancematchdialog.cpp#L79)），阻抗选「串联」形式（`rbSeries`）。
3. 输入频率 `1G`、Z.real `75`、Z.imag `0`；匹配类型下拉框保持第一项 `Series C - Parallel L`（.ui 文件中的两个选项之一，代码第 97 行 `currentIndex() == 0` 对应 `seriesC = true`）。
4. 记下给出的 L、C 值与 Matched 栏的残余回波损耗。此时 L/C 档位默认就是 `Ideal`（`lIdeal`/`cIdeal` 单选钮在 .ui 中默认 checked）。
5. 把 L、C 的档位从 `Ideal` 切到 `E24`，观察元件值被贴到哪个标准值、残余回波损耗恶化多少；再切到 `E6` 观察进一步恶化。

**需要观察的现象**：Ideal 档下应得到 C ≈ 4.5 pF、L ≈ 16.9 nH，matched 阻抗实部 ≈ 50、虚部 ≈ 0，回波损耗在 −60 dB 以下（受双精度舍入限制）；切到 E24 后 C 被贴到 4.3 或 4.7 pF（4.5 恰在两者正中，浮点比较决定归属），回波损耗显著变差（约 −36 dB 量级）。

**预期结果的手算依据**（对照 [Tools/impedancematchdialog.cpp:100-116](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/impedancematchdialog.cpp#L100-L116)）：Re(Z)=75 > Z0=50 走第一分支，\( B = \sqrt{1.5}\cdot\sqrt{75^2-50\cdot75} \approx 53.03 \)，串联 C 取负再除以 \( |Z|^2=5625 \) 得 \( B\approx -9.43\times10^{-3} \)；\( X = 1/B - Z_0/(B\cdot Re(Z)) \approx -35.36 \)。X<0 → \( C = -1/(\omega X) = 1/(2\pi\cdot10^9\cdot 35.36) \approx 4.5\,\text{pF} \)；B<0 → \( L = -1/(\omega B) \approx 16.9\,\text{nH} \)。验算：\( 75 \parallel j106.06 = 50 + j35.35 \)，再串 \( -j35.37 \) 得 \( 50 - j0.01 \approx 50\,\Omega \)。若 GUI 数字与此不符，优先检查你是否改过匹配类型（并联 C 分支会把 B 整体取反）。以上数值为公式代入结果，**具体显示位数待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么差模口阻抗是 \(2Z_0\)、共模口是 \(Z_0/2\)？

**答案**：差模激励时两口电压等幅反相：\(V_{diff}=V_1-V_3 = 2V\)，而两口电流大小相等方向相反，模式电流取 \(I_{diff}=(I_1-I_3)/2 = I\)，故 \(Z_{diff}=2V/I = 2Z_0\)。共模激励时两口电压相同、电流相加：\(V_{cm}=V\)，\(I_{cm}=2I\)，两口对外呈并联，\(Z_{cm}=Z_0/2\)。

**练习 2**：`ToESeries` 的 `while(index < 96 && ...)` 应该怎么修？

**答案**：把 96 换成 `series.size()`。这样对任何系列，循环条件中的 `series[index]` 访问都不会越界；同时 `higher` 的默认值 10.0 已经正确处理了「value 大于表内最大项」的情形（此时 higher 取下一十倍程的 10）。

**练习 3**：`MixedModeConversion` 生成的 16 条 Trace 属于 u8-l1 所说的哪一种数据来源？它们会随新的测量扫描自动更新吗？

**答案**：属于「数学」来源（`t->fromMath()`，第 74 行）。会更新：公式经 `setMathFormula` 挂到 Trace 数学链上，源 Trace（单端测量）每来新数据就会驱动公式重算（u8-l5 的 inputSamplesChanged 接力机制）。

---

### 4.3 自绘控件复用：SIUnitEdit 与 Unit

#### 4.3.1 概念说明

射频界面里到处是「输入一个带词头的数」：`1.5G`、`100k`、`-10dbm`。若每个对话框自己写一遍「解析尾字符→查表→乘因子」，既重复又易错。LibreVNA 的解法是两层拆分：

- **`Unit`（纯函数层）**：`ToString`/`FromString`/`SIPrefixToFactor`，不依赖任何界面，可单测；
- **`SIUnitEdit`（交互层）**：一个 QLineEdit，把「词头快捷键、滚轮调数、Esc 回滚」等交互习惯包起来，对外只暴露 `setValue`/`valueChanged`。

这套控件的复用度是全 GUI 最高之一：`SIUnitEdit` 在 GUI 源码中出现约 162 处、覆盖 26 个文件（grep 统计），从 VNA 扫描设置到去嵌入选项编辑器都在用。学会它，你就拿到了往自己的对话框里加「仪器级数值输入框」的现成零件。

#### 4.3.2 核心流程

`SIUnitEdit` 的交互模型有一个核心技巧：**当前值永远显示在 placeholderText 里，而不是 text 里**。

```
非编辑态：  placeholderText = "1.500000000 G"（灰色），text 为空
用户点击：  FocusIn → QTimer::singleShot(0) → continueEditing()
            → setText(placeholderText) + selectAll()
用户输入：  text = "2.5G"  （按 G 键的瞬间就触发 parseNewValue(1e9)）
提交：      Enter → parseNewValue(1.0) → 解析 → setValue(v*factor) → 发 valueChanged
放弃：      Esc   → clear() + setValueQuiet(_value)（恢复旧值）+ 发 editingAborted
失焦：      FocusOut → parseNewValue(1.0) + 发 focusLost
滚轮：      有焦点时按光标所在数位步进；无焦点时默认调第三位
```

解析函数 `parseNewValue(factor)` 的步骤：剥掉结尾的单位字符串 → 检查末字符是否在 `prefixes` 里（在则查 `SIPrefixToFactor` 得因子并削掉）→ 剩余部分 `toDouble` → `setValue(v * factor)`。

#### 4.3.3 源码精读

**接口一览**（[CustomWidgets/siunitedit.h:6-35](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.h#L6-L35)）：构造参数就是全部配置——`unit`（单位字符串）、`prefixes`（允许的词头集合，如 `" kMG"`）、`precision`（有效位）；三个 setter（`setUnit` 等）都顺手 `setValueQuiet` 刷新显示；信号有 `valueChanged`、`valueUpdated(QWidget*)`、`editingAborted`、`focusLost` 四个。

**构造与提交时机**（[CustomWidgets/siunitedit.cpp:11-29](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp#L11-L29)）：

```cpp
SIUnitEdit::SIUnitEdit(QString unit, QString prefixes, int precision, QWidget *parent)
    : QLineEdit(parent)
{
    ...
    installEventFilter(this);          // 自己监听自己的按键/滚轮/焦点
    connect(this, &QLineEdit::editingFinished, [this]() {
       parseNewValue(1.0);
    });
    setValueQuiet(0);
}
```

**防回环的 setValue**（[CustomWidgets/siunitedit.cpp:53-60](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp#L53-L60)）：只有值真的变了才发 `valueChanged`。若程序回写（例如设备 Limits 夹取后的值，u7-l1）与当前值相同，信号被压掉，避免「设置→信号→再设置」的无限循环。`setValueQuiet` 则永远不发信号，专用于程序侧静默刷新。

**事件过滤器：三组快捷交互**（[CustomWidgets/siunitedit.cpp:72-106](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp#L72-L106)）：

```cpp
if(key == Qt::Key_Escape) {          // 放弃编辑，恢复旧值
    clear(); setValueQuiet(_value); emit editingAborted(); ...
}
if(key == Qt::Key_Return || ...) {   // 无词头提交
   parseNewValue(1.0); continueEditing(); ...
}
...
if (prefixes.indexOf(static_cast<QChar>(key)) >= 0) {
    // a valid prefix key was pressed
    parseNewValue(Unit::SIPrefixToFactor(key));   // 按词头键立即提交
```

注意大小写兜底 `swapUpperLower`（第 62-70 行）：用户没按 Shift 时，小写 `g` 也能匹配到大写 `G` 词头（反之亦然）——`m`（毫）与 `M`（兆）相差九个数量级，这个兜底不能无脑互换，所以只在「按下的键不在表里，而其大小写互换后的键在表里」时才启用。

**滚轮按位调数**（[CustomWidgets/siunitedit.cpp:114-163](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp#L114-L163)）：有焦点时，步长由光标位置换算出的「第几位数字」决定——\( \text{step} = 10^{\lfloor \lg|v| \rfloor - n + 1} \)，光标越靠右步长越小，等于把光标当「数字万用表的量程旋钮」；无焦点时默认调第三位。光标在第 0 位（小数点前）时直接忽略，防止一次滚动造成数量级跳变。

**placeholderText 技巧**（[CustomWidgets/siunitedit.cpp:167-175](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp#L167-L175)）：

```cpp
void SIUnitEdit::setValueQuiet(double value) {
    _value = value;
    setPlaceholderText(Unit::ToString(value, unit, prefixes, precision));
    if(!text().isEmpty()) {
        // currently editing, update the text as well
        continueEditing();
    }
}
```

好处有两个：placeholder 不进入编辑内容，用户点击时 `selectAll()` 一次就能整体覆盖，不会把 `"1.500000000 G"` 当成待编辑文本；而且「正在编辑」这个状态可以简单地用 `text().isEmpty()` 判断。

**纯函数层 Unit**（[unit.cpp:34-97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/unit.cpp#L34-L97)）：`ToString` 自动挑选让整数位不超过 3 的词头（第 49-54 行的 `preDotDigits` 循环），NaN/Inf 显示 "NaN"，零显示 "0"；`SIPrefixToFactor`（第 81-97 行）就是一张 `f/p/n/u/m/' '/k/M/G/T/P` 到 10 的幂的查表。解析侧 `FromString`（第 10-32 行）与 `SIUnitEdit::parseNewValue` 共享同一套「剥单位→查词头→toDouble」逻辑。

**复用样板**：阻抗匹配对话框只用了五行就把三个输入框配置成 Ω/Hz 编辑器（[Tools/impedancematchdialog.cpp:19-31](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/impedancematchdialog.cpp#L19-L31)）：

```cpp
ui->zReal->setUnit("Ohm");
ui->zFreq->setUnit("Hz");
ui->zFreq->setPrefixes(" kMG");
...
ui->lValue->setUnit("H");
ui->lValue->setPrefixes("pnum ");
```

这也是 .ui 文件里放一个提升为 `SIUnitEdit` 的 `QLineEdit`、再在代码里补配置的典型用法。另外 `sizeHint`（[CustomWidgets/siunitedit.cpp:31-51](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp#L31-L51)）会按「最宽词头 + precision 个 8」预估宽度，保证布局不因单位切换而跳动——做自己的控件时值得抄。

#### 4.3.4 代码实践

**实践目标**：亲身体验 `SIUnitEdit` 的全部交互路径，并把每条路径对应到源码中的一段处理逻辑。

**操作步骤**：

1. 启动 GUI（无设备即可），进入 VNA 模式，找到任一使用 `SIUnitEdit` 的输入框（例如主界面上的频率设置框；`VNA/vna.cpp` 中有约 21 处 `SIUnitEdit` 引用）。
2. 依次完成六个动作并记录结果：
   - 输入 `1G` 后按回车；
   - 输入 `1000000000` 后按回车（观察两种输入是否等价）；
   - 输入到一半按 `Esc`（观察是否恢复旧值）；
   - 输入 `2.5` 后直接按 `G` 键（不按回车，观察是否立即生效）；
   - 在框内滚动滚轮，分别在光标位于首位、中间、末位时各试一次（观察步长差异）；
   - 点击框外让焦点离开（观察半输入的内容是否被提交）。
3. 对照 [CustomWidgets/siunitedit.cpp:72-165](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp#L72-L165)，为六个动作各写下触发它的代码分支（Esc→第 77-83 行；Enter→第 84-89 行；词头键→第 91-106 行；FocusOut→第 107-110 行；滚轮→第 114-163 行）。

**需要观察的现象**：所有输入途径（带词头、纯数字、词头快捷键、滚轮）最终都收敛到同一条路径——`parseNewValue` → `setValue` → `valueChanged` 信号；`Esc` 是唯一「不产生新值」的出口。

**预期结果**：你能画出一张「事件 → eventFilter 分支 → parseNewValue 的 factor 实参 → setValue」的对照表。这是一次纯 GUI 实践，无需设备；若你在无头模式（`--no-gui`）下运行则没有可点击的界面，此时改为纯源码阅读：跟踪 `Unit::FromString("1.5G", "Hz", " kMG")` 的返回值应为 `1.5e9`（对照 [unit.cpp:10-32](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/unit.cpp#L10-L32) 逐行手推）。

#### 4.3.5 小练习与答案

**练习 1**：为什么把当前值放在 placeholderText 而不是直接 `setText`？

**答案**：placeholder 不是编辑内容。若用 `setText`，用户点击进入编辑时要先手动选中并删掉 `"1.500000000 G"` 整串字符，而且这串带单位文本本身可能被再次解析；用 placeholder 后 `selectAll()` 一键覆盖即可，同时「text 是否为空」天然成为「是否正在编辑」的标志位。

**练习 2**：`setValue` 与 `setValueQuiet` 的区别是什么？各适合什么场景？

**答案**：`setValue` 只在值变化时发 `valueChanged`/`valueUpdated`，适合用户输入这条主动路径（防抖动、防回环）；`setValueQuiet` 永不发信号，适合程序侧静默刷新（如 `setUnit` 改单位后重绘、Esc 恢复旧值）。如果程序回写误用 `setValue`，就可能形成「A 设置 B、B 通知 A」的信号环。

**练习 3**：`prefixes` 字符串里为什么常常有一个空格（如 `"pnum "`、`" kMG"`）？

**答案**：空格对应 `SIPrefixToFactor` 里的 `case ' ': return 1e0;`，即「无词头」也是合法选项。这允许用户输入 `0.1 `（或程序显示无词头形式）时仍能正确解析/显示；同时 `ToString` 从 prefixIndex=0 起步，第一个词头字符就是候选起点。

---

## 5. 综合实践

**任务：写一个独立的小 demo，把「混合模式转换」和「阻抗匹配」两个工具串成一条链，并用 parameters 的矩阵函数交叉验证。**

先说清一个事实：`MixedModeConversion` 的 16 条公式以字符串形式内联在 GUI 对话框里（4.2.3），库层面**没有**可调用的混合模式函数——所以本实践的第一个动作就是「复刻」，第二个动作才是「验证」，第三步可以顺手把它重构成 parameters 库的自由函数（这正是开源贡献的标准切口）。

**背景设定**：被测件是两条互不耦合的理想匹配直通线——线 A 连端口 1↔2，线 B 连端口 3↔4。单端 S 矩阵只有 \(S_{21}=S_{12}=S_{43}=S_{34}=1\)，其余全 0。物理上它对差模与共模一视同仁，所以正确答案可以预先手算：\(S_{DD21}=S_{CC21}=1\)（两种模式都无损耗通过），\(S_{DD11}=0\)（无反射），\(S_{DC21}=S_{CD21}=0\)（模式间无转换）。**注意规格里说的「2 端口 S 参数」不足以做混合模式转换**——差分测量需要 4 端口单端数据（单台 LibreVNA 用 CompoundDriver 聚合，或直接导入 4 端口 Touchstone，u8-l4），这也是 GUI 对话框要求选择 S11~S44 全部 16 条 Trace 的原因。

**第 1 步：复刻公式（逐行对照 mixedmodeconversion.cpp:119-137）**

```cpp
// 示例代码：可加入 LibreVNA-Test 的 ParameterTests（u11-l1 的五步补洞法），
// 亦可临时改成带 main() 的独立程序
#include "Tools/parameters.h"
#include <complex>
#include <vector>

using Type = std::complex<double>;

std::pair<Type, Type> mixedModeDemo()
{
    // 1) 构造 4 端口单端 S 参数：两条理想直通线
    Sparam S(4);
    S.set(2, 1, Type(1.0));   // S21 = 1  （线 A：端口 1 -> 2）
    S.set(1, 2, Type(1.0));   // S12 = 1
    S.set(4, 3, Type(1.0));   // S43 = 1  （线 B：端口 3 -> 4）
    S.set(3, 4, Type(1.0));   // S34 = 1
    // 其余元素由构造函数置 0

    // 2) GUI 公式逐条复刻（对照 Tools/mixedmodeconversion.cpp:119-137）
    auto SDD11 = 0.5 * (S.get(1,1) - S.get(1,3) - S.get(3,1) + S.get(3,3));
    auto SDD21 = 0.5 * (S.get(2,1) - S.get(2,3) - S.get(4,1) + S.get(4,3));
    auto SCC21 = 0.5 * (S.get(2,1) + S.get(2,3) + S.get(4,1) + S.get(4,3));
    auto SDC21 = 0.5 * (S.get(2,1) + S.get(2,3) - S.get(4,1) - S.get(4,3));
    return {SDD11, SDD21};
}
```

**第 2 步：用矩阵变换交叉验证。** 16 条公式其实是 \( S_{mm} = M S M^{\mathsf T} \) 的逐元素展开，其中 \(M\) 是正交的模式变换阵（\(M M^{\mathsf T}=I\)，\(1/\sqrt{2}\times 1/\sqrt{2}=0.5\) 正是公式里的系数）：

\[ M = \frac{1}{\sqrt{2}}\begin{bmatrix} 1&0&-1&0\\ 0&1&0&-1\\ 1&0&1&0\\ 0&1&0&1 \end{bmatrix} \]

行序为 [差模口1, 差模口2, 共模口1, 共模口2]，于是 \( (M S M^{\mathsf T})_{21} \) 就等于 \(S_{DD21}\)。利用 `data` 是 public 的这一设计，直接用 Eigen 写：

```cpp
// 示例代码（续）：矩阵法交叉验证
Eigen::MatrixXcd M = Eigen::MatrixXcd::Zero(4,4);
const double s = 1.0 / std::sqrt(2.0);
M <<  s,0,-s,0,
      0,s,0,-s,
      s,0, s,0,
      0,s,0, s;
Eigen::MatrixXcd Smm = M * S.data * M.transpose();
// Smm(1,0) 应等于 SDD21，Smm(3,0) 应等于 SCC21 ...
```

断言四条预期：`SDD11 == 0`、`SDD21 == 1`、`SCC21 == 1`、`SDC21 == 0`，并断言公式法与矩阵法逐元素一致。若放进 `LibreVNA-Test`，遵循 u11-l1 的容差经验：这里全是精确值，用 `QVERIFY(qFuzzyCompare(...))` 即可；改成有耦合的线（把 \(S_{13}\) 设成 0.1 之类的非零值）后，数值不再「干净」，需要回到相对容差思维。

**第 3 步：接到阻抗匹配。** 假设同一 DUT 的差模口反射测得 \(S_{DD11}=0.2\)（比如线 A/B 长度不对称时就会出现）。按 `impedancematchdialog.cpp:73-77` 的同一行公式换算差模阻抗，注意参考阻抗必须换成差模口的 \(Z_{0,\text{diff}}=2Z_0=100\,\Omega\)：

\[ Z_{diff} = 100\cdot\frac{1+0.2}{1-0.2} = 150\,\Omega \]

然后在 GUI 里 `Tools → Impedance Matching`，手工输入 Z = 150 Ω @ 1 GHz（保持串联形式、匹配类型第一项），读出元件值并按 4.2.4 的方法手算核对。**一个值得写进你实验报告的发现**：对话框手工输入分支把 Z0 硬编码为 50 Ω（[Tools/impedancematchdialog.cpp:79](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/impedancematchdialog.cpp#L79)），它不知道「差模口」这回事——从 Marker 读数时会用 Trace 的参考阻抗（混合模式转换已正确设成 2Z₀），但手工输入差模阻抗时匹配目标仍是 50 Ω。差模匹配的正确姿势是：要匹配到 50 Ω 单端，先算 \(Z_{diff}\)，或按 100 Ω 目标自行缩放。发现并说清这种「工具边界」，比工具本身更能体现你读通了代码。

**交付物**：demo 代码 + 四条断言的结果 + 一段 200 字说明「为什么两条不耦合的线没有模式转换，而任何不对称都会产生 \(S_{DC}\neq 0\)」。

## 6. 本讲小结

- **parameters 数学库是「一个矩阵五种视图」**：基类 `Parameters` 持有唯一的 `Eigen::MatrixXcd data` 并提供 1 基 `get/set`；S/Z/Y/ABCD/T 五个子类靠转换构造函数互换，级联用矩阵乘法，N 端口换域用 Eigen 的 `inverse()`，固定 2×2 的求逆与开方则手写以避开动态矩阵的开销——按问题规模选工具是 Eigen 在本项目的正确打开方式。
- **多端口日常工具**：`swapPorts` 换行列、`reduceTo` 裁子矩阵，它们是把 4 端口数据喂给 2 端口算法（twothru、校准测量）之前的必经一步。
- **三个 Tools 菜单工具构成一条加工链**：`MixedModeConversion` 把 4 端口单端数据按 0.5 系数的 16 条公式（配对约定 (1,3)/(2,4)，参考阻抗 D→2Z₀、C→Z₀/2）翻译成差模/共模 Trace；`ImpedanceMatchDialog` 用 \(Z=Z_0(1+\Gamma)/(1-\Gamma)\) 把 Marker 读数变成阻抗，再设计双元件 L 网络并回算残余反射；`ESeries` 负责把理想值贴到可购买的标准值。
- **读代码要保持怀疑**：`ToESeries` 的循环上界 96 只对 E96 安全、`Tparam` 构造函数抛出的错误信息写着 "ABCD"、阻抗匹配对话框手工输入分支硬编码 Z0=50——三处都不是阻塞性 bug，但都是「代码与现实有出入」的实证。
- **SIUnitEdit + Unit 是复用率最高的自定义控件**（约 162 处使用）：纯函数层管解析/格式化，交互层用「placeholderText 存当前值 + 事件过滤器」实现词头快捷键、Esc 回滚、按位滚轮；`setValue` 只在值变化时发信号以阻断回环。

## 7. 下一步学习建议

1. **下一讲 u11-l3（毕业实战）**：把本手册的三层数据链路知识串起来，从零实现一个最小 `DeviceDriver`（虚拟演示设备）。本讲的 `Sparam` 矩阵构造正好可以用来给你的 DemoDriver 生成假数据（比如按频率滚动的合成 S 参数）。
2. **回看消费者**：带着本讲的参数类知识重读 u9-l5 的 [VNA/Deembedding/matchingnetwork.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/matchingnetwork.cpp)（ABCD 级联与求逆的密集使用）与 u9-l2 的 TRL 求解（T 参数），你会看到 `operator*`/`inverse`/`root` 每一个都对应一处物理操作。
3. **一个顺手的小贡献**：综合实践中已经指出，混合模式公式没有进数学库。试着把它提取成 `parameters.h` 的自由函数（如 `Sparam mixedModeConversion(const Sparam&)`），配上 u11-l1 风格的单元测试（矩阵法做期望值来源正好满足「期望值须来自被测代码之外」），这就是一次完整而低风险的开源提交流程演练。
