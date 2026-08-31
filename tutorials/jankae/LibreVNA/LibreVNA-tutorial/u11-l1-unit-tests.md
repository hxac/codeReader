# 单元测试与数值验证

> **模块映射**：4.1 对应「测试工程结构」，4.2 与 4.3 对应「校准与参数测试用例」，4.4 对应「新增用例方法」。

## 1. 本讲目标

学完本讲，你应当能够：

1. **搭建并运行** LibreVNA-Test 测试工程：全量运行、只跑一个测试类、只跑一个测试函数，并看懂 Qt Test 的输出与退出码。
2. **读懂现有用例**：说出参数测试、校准测试、FFT 测试各自「期望值从哪里来」，并识别每种策略的强弱点（手算常数、对偶往返、公式镜像、合成数据、边界值）。
3. **独立新增一个用例**：为 `Tools/parameters.h` 中尚未被覆盖的 S↔T 参数转换函数手算期望值、写出测试、编译运行直至通过，并能把自己用例的风格与现有用例做对比分析。

本讲是「专家层」的入口：前面十一个单元教你怎么**读**这套代码，本讲开始教你怎么**证明**它是对的——以及怎么证明你自己新写的代码是对的。

## 2. 前置知识

### 2.1 什么是「已知答案」测试

LibreVNA 里最有价值的代码是**数学代码**：S 参数矩阵转换、校准误差项求解、FFT、圆拟合。这类代码的单元测试有一个统一范式——**已知答案测试（oracle test）**：

```
构造输入（通常是一个小到能手算的值）
    ↓
调用被测函数
    ↓
把输出与「事先手算/查表得到的期望值」比对
```

关键在「事先」二字：期望值必须**独立于被测代码**得出。如果期望值是让被测代码自己跑一遍抄下来的，测试就永远绿色、毫无意义。本讲会反复回到这个原则，并指出仓库里一处「轻微违反」它的用例（见 4.3.3 的 portextensiontests）。

### 2.2 Qt Test 最小骨架

Qt 自带的测试框架只需要三样东西：

1. 一个继承 `QObject` 的类，带 `Q_OBJECT` 宏；
2. 类里声明一个 `private slots:` 区域——**每个 slot 函数就是一个测试用例**，框架靠 Qt 元对象系统自动发现它们，不需要手工注册清单；
3. `main()` 里调用 `QTest::qExec(new MyTests, argc, argv)` 执行。

断言常用两个宏：

- `QVERIFY(条件)`——条件为假则该用例失败并记录行号；
- `QCOMPARE(实际值, 期望值)`——除了失败还能打印两个值，便于诊断。

后面 4.2.3 会看到，本仓库几乎只用 `QVERIFY`，这本身就是一个值得讨论的风格取舍。

### 2.3 浮点数不能用 `==`：`qFuzzyCompare` 家族

数学代码的输出是浮点数，而 `0.1 + 0.2 != 0.3`（二进制浮点无法精确表示大部分十进制小数）。Qt 提供模糊比较，其语义是**相对误差**：

\[ |p_1 - p_2| \;\le\; \varepsilon \cdot \min(|p_1|, |p_2|), \qquad \varepsilon_{\text{double}} = 10^{-12},\; \varepsilon_{\text{float}} = 10^{-5} \]

注意两个推论，它们解释了仓库测试里所有「奇怪」的写法：

- **分母是 min(|p1|,|p2|)，一旦其中一个值为 0，右边就恒为 0**——于是 `qFuzzyCompare(x, 0.0)` 几乎永远失败（除非 x 精确等于 0）。**与 0 比较必须用 `qFuzzyIsNull`**（判 |x| ≤ 阈值）。
- double 版的 \(10^{-12}\) 太苛刻，复数矩阵求逆这类多步运算的累积误差常常超过它——所以仓库测试大量使用 **先转 float 再比较** 的技巧（见 4.2.3）。

### 2.4 承接前面各讲

- u1-l2 已经建立认知：`LibreVNA-Test` 是**纯 PC 侧单元测试**，不连接真实硬件；需要硬件的端到端测试在 `Software/Integrationtests/`（u10-l3 已讲）。
- u1-l3 讲过 qmake6/make 的通用构建流程，本讲直接套用。
- u8-l6 精读过 `ffttests.cpp` 的两个细节（5 点数据走 Bluestein 分支、逆变换后 `/size` 的缩放契约），本讲只做方法学归纳，不再展开数学。
- u9-l2 已带你跑过 `CalibrationTests` 单类；本讲补全「全量运行、单函数运行、退出码」等完整工作流。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [Software/PC_Application/LibreVNA-Test/main.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/main.cpp) | 测试入口：创建 QApplication，串联 6 个测试类的 qExec |
| [Software/PC_Application/LibreVNA-Test/LibreVNA-Test.pro](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/LibreVNA-Test.pro) | 工程文件：声明测试框架依赖，并把整个 GUI 编进测试二进制 |
| [Software/PC_Application/LibreVNA-Test/parametertests.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp) | S/Z/ABCD 参数转换测试（本讲精读 + 补测试的对象） |
| [Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp) | 被测的参数转换实现，含**未被测试覆盖的 S↔T、swapPorts、reduceTo** |
| [Software/PC_Application/LibreVNA-Test/calibrationtests.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp) | 校准频率栅格检测测试（假数据工厂范式） |
| [Software/PC_Application/LibreVNA-Test/ffttests.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/ffttests.cpp) | FFT 引擎测试（含一个**空的占位用例**） |
| [Software/PC_Application/LibreVNA-Test/utiltests.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/utiltests.cpp) | 圆拟合、固件版本比较、阻抗↔S 参数测试（边界值范式） |
| [Software/PC_Application/LibreVNA-Test/impedancerenormalizationtests.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/impedancerenormalizationtests.cpp) | 阻抗再归一化去嵌入选项的测试 |
| [Software/PC_Application/LibreVNA-Test/portextensiontests.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/portextensiontests.cpp) | 端口延伸自动提取与修正的测试（合成数据范式） |

## 4. 核心概念与源码讲解

### 4.1 测试工程结构：一个把整个 GUI 链接进来的测试二进制

#### 4.1.1 概念说明

初学者想象的「单元测试」是：只编译被测的那一个 `.cpp` 加一个 `main()`，几秒钟出结果。LibreVNA-Test **不是**这种。

它是一个**把几乎整个 GUI 应用（约 160 个源文件）连同固件侧 `Protocol.cpp` 一起编进来的测试程序**。这么做的动机很实际：`Calibration`、`PortExtension`、`ImpedanceRenormalization` 这些被测类并非孤立的头文件 + 实现，它们的构造函数、JSON 反序列化、甚至 `edit()` 都会触碰 `Trace`、`DeviceDriver`、`CalStandard` 等一大片类型。与其为测试做一层假的编译边界，不如直接复用 GUI 的 `.pro` 内容。

代价是编译时间与 GUI 相当；收益是**测试里可以直接使用生产代码的全部类型**，写起来毫无阻抗。理解这一点，你才能理解下面两个「怪现象」：

- `main.cpp` 创建的是 `QApplication`（GUI 应用对象）而不是 `QCoreApplication`；
- 跑测试时屏幕上可能**真的闪现出对话框**。

#### 4.1.2 核心流程

构建期（与 u1-l3 的 GUI 构建完全同构）：

```
cd Software/PC_Application/LibreVNA-Test
qmake6 LibreVNA-Test.pro      ← 生成 Makefile（QT += testlib 起作用）
make -j$(nproc)               ← 编译 GUI 全部源文件 + 7 个测试文件
./LibreVNA-Test               ← 运行
```

运行期：

```
main() 创建 QApplication
  → qExec(UtilTests)            返回 0 或非 0
  → qExec(PortExtensionTests)   结果按位或累积
  → qExec(ParameterTests)
  → qExec(fftTests)
  → qExec(ImpedanceRenormalizationTests)
  → qExec(CalibrationTests)
  → return status               0 = 全部通过
```

每个 `qExec` 会解析命令行参数，所以你可以**在运行时选择执行范围**：

```
./LibreVNA-Test                          # 全部 6 类
./LibreVNA-Test ParameterTests           # 只跑一个类
./LibreVNA-Test ParameterTests::S2ABCD   # 只跑一个用例
./LibreVNA-Test -functions               # 列出所有用例名（不含运行）
make check                               # CONFIG += testcase 提供的目标
echo $?                                  # 0 = 全绿
```

#### 4.1.3 源码精读

**入口：一个 QApplication 串六个 qExec**

[main.cpp:10-23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/main.cpp#L10-L23) 是整个测试工程的全部入口，这段代码做了三件事：第 12 行创建 `QApplication`（因为多个测试会实例化真实控件，事件循环由 qExec 内部维护）；第 15-20 行按固定顺序执行 6 个测试类，用 `|=` 把每次的退出码按位或起来——**任何一类失败，最终返回值就非 0**，这是脚本能用退出码判断全绿的原因；第 22 行返回累积状态。

注意第 14-20 行的顺序是「从纯函数到重控件」排列：`UtilTests`（纯数学）在前，`CalibrationTests`（要构造校准件对象图）在后。这个顺序没有功能意义，但读起来有层次感。

**工程文件：三个关键声明**

[LibreVNA-Test.pro:1](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/LibreVNA-Test.pro#L1) 的 `QT += testlib widgets network svg` 引入 Qt Test 模块（`QVERIFY`/`qExec` 所在），同时保留 widgets——再次印证「测试会碰真实控件」。

[LibreVNA-Test.pro:3](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/LibreVNA-Test.pro#L3) 的 `CONFIG += ... testcase` 让 qmake 额外生成 `make check` 目标，方便 CI 一键跑测试。

[LibreVNA-Test.pro:8-9](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/LibreVNA-Test.pro#L8-L9) 把固件侧的 `Protocol.cpp` 也编进来——与 GUI 的 `.pro` 做法一致（u1-l3 讲过「协议两端同源编译」），测试二进制因此也携带同一份协议定义。

至于被测代码：第 13-163 行是 GUI 的几乎全部源文件（`../LibreVNA-GUI/...` 相对路径引用），第 164-170 行才是 6 个测试文件自己。**这带来一个对你极其重要的推论**：只要你把新测试写进**已存在的**测试文件（如 `parametertests.cpp`），**完全不需要改 `.pro`**——新增用例的门槛被降到最低。

**支线：为什么跑测试会弹出对话框**

[portextensiontests.cpp:35](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/portextensiontests.cpp#L35) 直接调用了被测对象的 `edit()`——这是「打开编辑对话框」的函数。[portextension.cpp:63-139](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp#L63-L139) 显示它会 `new QDialog()`、`setupUi`、连接一堆信号槽，最后在 [第 136-138 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp#L136-L138) 以 `if(AppWindow::showGUI()) dialog->show()` 收尾。

`show()` 是**非模态**显示，不会阻塞；而 `showGUI()` 的实现在 [appwindow.cpp:1288-1291](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1288-L1291)，只是返回 `!noGUIset`——测试进程没人设置过 `--no-gui` 标志，所以条件为真，对话框会被显示出来。测试之所以还能继续跑，全靠「非模态 + 事件循环由 qExec 驱动」。这算是一个历史包袱式的写法：更干净的做法是测试里根本不调 `edit()`，只测纯逻辑。

#### 4.1.4 代码实践

**实践 A：编译、运行并盘点结果**

1. **目标**：亲手建立「改代码 → 跑测试」的反馈回路，并留下一份基线记录。
2. **步骤**：
   - 确认依赖与 u1-l3 编译 GUI 时完全相同（Qt6 + libusb），无需额外安装；
   - 执行：
     ```bash
     cd Software/PC_Application/LibreVNA-Test
     qmake6 LibreVNA-Test.pro && make -j$(nproc) 2>&1 | tail -5
     ./LibreVNA-Test 2>&1 | tee test-baseline.txt
     echo "exit code: $?"
     ./LibreVNA-Test -functions
     ```
3. **观察现象**：Qt Test 对每个用例输出一行 `PASS` 或 `FAIL`，每类结束有 `Totals: X passed, Y failed`；`-functions` 列出形如 `ParameterTests::S2ABCD()` 的全限定名；跑 `PortExtensionTests` 前后可能会闪现编辑对话框。
4. **预期结果**：6 个测试类、共 21 个非空用例全部 PASS，退出码 0。若你的环境缺 Qt6 testlib 模块（如 `Project ERROR: Unknown module(s) in QT: testlib`），需先补装 Qt6 测试组件——**完整清单待本地验证**（不同发行版包名不同，如 Debian 系为 `qt6-base-dev` 所含或 `libqt6test6`）。
5. 把 `test-baseline.txt` 存好，4.4 的实践要用它做前后对比。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把 6 个测试类合并成一个大类？
**答案**：分开的类各自拥有独立的构造/析构与命名空间（`private slots` 里的函数名可以重复，如各类都可以有 `correct()`），并且 `qExec` 支持按类名单独执行——合并后就不能 `./LibreVNA-Test ParameterTests` 这样精准重跑，失败定位也要在几百个用例里翻找。

**练习 2**：`status |= QTest::qExec(...)` 里为什么用按位或而不是 `status = ...`？
**答案**：要保证「任何一个类失败，最终退出码就非 0」。若直接赋值，最后一类的 PASS 会覆盖前面所有失败，CI 会误判全绿。这是多 qExec 程序的标准写法。

**练习 3**：测试二进制里链接了 `librevnausbdriver.cpp` 等真实 USB 驱动代码，测试会不会连上设备？
**答案**：不会。链接了代码不等于执行了代码——所有用例都只调用纯计算路径（参数转换、栅格检测、FFT），没有任何用例触发设备枚举或 `connectTo`。需要真机的验证在 `Software/Integrationtests/`（u10-l3）。

### 4.2 参数测试用例：期望值从哪里来的五种策略

#### 4.2.1 概念说明

`parametertests.cpp` 测的是 `Tools/parameters.cpp` 里的 S/Z/ABCD 参数矩阵互转——这些公式在 u9-5（去嵌入）里已经被你当工具用过，现在换一个视角：**怎么知道这些公式实现对了？**

这类测试的全部难点浓缩成一个问题：**期望值从哪里来？** 本仓库用了五种策略，各有适用场景与陷阱，整理如下表（这是本讲最重要的方法论输出）：

| 策略 | 做法 | 优点 | 陷阱 | 仓库示例 |
|---|---|---|---|---|
| ① 手算常数 | 把高精度期望值硬编码进源码 | 完全独立于被测代码，最可信 | 值本身抄错则测试错 | `S2ABCD` |
| ② 对偶往返 | A→B→A，断言回到起点 | 不需要外部值，天然覆盖两个方向 | 两处错误可能互相抵消 | `ABCD2S` |
| ③ 公式镜像 | 在测试里**重新实现**教科书公式再比对 | 顺便文档化了公式 | 与实现抄同一处错则同错 | `S2Z_2P`、`Z2S_2P` |
| ④ 参数扫描 | 对 Z0 = 10..100 循环重复断言 | 覆盖参数空间，暴露量纲错误 | 计算量大时慢 | 所有 Z/S 用例 |
| ⑤ 解析边界 | 挑物理上有精确解的输入（50Ω→0、开路→1） | 期望值可心算，无浮点争议 | 覆盖面窄 | `utiltests` 的阻抗部分 |

好的测试套件会**组合**使用：①给锚点、②给广度、⑤给边界。

#### 4.2.2 核心流程

单个参数测试的执行流程：

```
用 complex literals 写死 4 个复数（S11/S12/S21/S22）
  → 构造 Sparam 值类型（内部是 Eigen::MatrixXcd）
  → 调被测构造函数（如 ABCDparam(S, 50.0)）完成转换
  → 对结果的 4 个元素 × 实/虚部共 8 个数逐一 QVERIFY
```

被测对象都是**值类型**（无状态、无 IO），所以测试不需要 setup/teardown——这正是「纯函数最好测」的体现。

#### 4.2.3 源码精读

**策略①：S2ABCD 的手算常数**

[parametertests.cpp:12-36](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L12-L36) 先在第 16-19 行写死一组 S 参数（形如 `0.0038 + 0.0248i` 的典型「近似理想直通」测量值），第 20-21 行完成 S→ABCD 转换；随后 [第 23-26 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L23-L26) 的期望值是**17 位有效数字的高精度常数**——这显然来自某个外部可信计算源（如 Python/scikit-rf 的参考实现），而非手抄被测代码输出。第 28-35 行对 4 个矩阵元素的实部虚部分别 `qFuzzyCompare`，共 8 条断言。

**策略②：ABCD2S 的对偶往返**

[parametertests.cpp:38-63](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L38-L63) 与 S2ABCD **互为逆过程**：输入是上一测的 ABCD 期望值，期望值是上一测的 S 输入。两个用例合起来构成「S→ABCD→S」的往返闭环，且两边的期望值都独立硬编码——这比单纯 `S→X→S` 断言相等更强，因为逐元素的值都被外部锚定过。

**策略③＋④：S2Z_1P 的公式镜像 + 阻抗扫描**

[parametertests.cpp:65-83](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L65-L83) 是**单端口** S→Z 用例，麻雀虽小五脏俱全：

- [第 73 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L73) 对 Z0 从 10 到 100 每 10Ω 扫一遍（策略④）——参考阻抗错了量纲或漏乘 Z0，扫描会立刻暴露；
- [第 77 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L77) 在测试里写出教科书单端口公式：

\[ Z_{11} = Z_0\,\frac{1+S_{11}}{1-S_{11}} \]

- [第 79-81 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L79-L81) 的注释直白地解释了 float 技巧：**「浮点误差对 qFuzzyCompare(double,double) 太大，改用 qFuzzyCompare(float,float)」**——double 转 float 截断到约 7 位有效数字，加上 float 版 10⁻⁵ 的相对容差，让多步复数矩阵运算的累积误差得以通过。

[parametertests.cpp:85-116](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L85-L116) 的 `S2Z_2P` 是双端口版本，[第 100-104 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L100-L104) 镜像了完整的双端口公式族（以 ΔS 为公分母）：

\[ \Delta_S = (1-S_{11})(1-S_{22}) - S_{12}S_{21}, \qquad Z_{11} = \frac{(1+S_{11})(1-S_{22}) + S_{12}S_{21}}{\Delta_S}\,Z_0 \]

而 [parametertests.cpp:118-169](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L118-L169) 的 `Z2S_1P/Z2S_2P` 用同一组策略测反方向，[第 130 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L130) 的镜像公式为 \( S_{11} = \dfrac{Z_{11}/Z_0 - 1}{Z_{11}/Z_0 + 1} \)。

**对照被测实现**：[parameters.cpp:186-208](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L186-L208) 的 `Zparam(S, Z0n)` 并没有用教科书标量公式，而是走**通用矩阵形式** \( Z = \sqrt{z}\,(1+S)(1-S)^{-1}\sqrt{z} \)（第 191-207 行的注释与实现），靠 Eigen 的矩阵求逆一次支持任意端口数。测试用标量公式镜像矩阵实现——**两条独立路径殊途同归**才是这类测试的价值所在，恰好规避了策略③「抄同一公式」的陷阱。

**基类索引细节**：[parameters.h:23-25](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.h#L23-L25) 的 `get(row, col)` 是**1 基索引**（文献惯例），内部再减一映射到 Eigen 的 0 基——所有测试断言里的 `get(1,1)`、`get(2,1)` 都遵守这个约定。

#### 4.2.4 代码实践

**实践 B：亲手复核一个期望值，验证「锚点」可信**

1. **目标**：不依赖被测代码，独立算出 `S2Z_1P` 在 Z0=50Ω 时的期望值，确认测试锚点真实可靠。
2. **步骤**：
   - 取 [parametertests.cpp:69](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L69) 的输入 \( S_{11} = 0.0038 + 0.0248i \)；
   - 按公式 \( Z_{11} = 50 \cdot \frac{1+S_{11}}{1-S_{11}} \) 手算（或用 Python 复数运算交叉验证）：
     - 分子 \( 1.0038 + 0.0248i \)，分母 \( 0.9962 - 0.0248i \)；
     - 除法展开后约得 \( 1.00639 + 0.04995i \)；
     - 乘 50 得 \( Z_{11} \approx 50.32 + 2.497i \ \Omega \)。
3. **观察现象**：把这三个数记下来。
4. **预期结果**：这个值就是被测 `Zparam` 在该输入下**应当**给出的结果。如果你接着写一个临时断言 `QVERIFY(qFuzzyCompare((float)Z.get(1,1).real(), 50.32f))`，它应当通过——即你的手算、测试的镜像公式、Eigen 矩阵实现三者一致。手算过程与运行结果均为**待本地验证**（复数除法手算容易错位，建议用 `python3 -c "print(50*(1+(0.0038+0.0248j))/(1-(0.0038+0.0248j)))"` 复核）。
5. 体会：**你能手算，测试才可信**——这就是「已知答案」的含金量。

#### 4.2.5 小练习与答案

**练习 1**：`S2ABCD` 里为什么对 4 个元素×2（实虚部）写 8 条 `QVERIFY`，而不是一条 `QVERIFY(abcd.get(1,1) == A)`？
**答案**：两个原因。其一，复数 `==` 是精确比较，浮点结果几乎必然不等；其二，`qFuzzyCompare` 没有复数重载，只能拆成实虚部分别比。拆开还有个附带好处：失败时能立刻看出是哪个分量错了。

**练习 2**：策略③「公式镜像」的最大风险是什么？本仓库如何缓解？
**答案**：风险是实现与测试抄了同一处错误的公式（比如同一本写错的书），测试仍然全绿。缓解办法是让两边走**不同路径**：实现用 Eigen 矩阵广义公式（`Z = √z(1+S)(1-S)⁻¹√z`），测试镜像的是标量教科书公式；再加上 `S2ABCD` 完全独立的高精度常数锚点，三路互证。

**练习 3**：如果想知道 parametertests 覆盖了 parameters.cpp 的多少比例，最快的办法是什么？
**答案**：对照函数清单做减法。`parametertests.cpp` 只测了 `Sparam(ABCD)`、`ABCDparam(S)`、`Zparam(S)`、`Sparam(Z)` 四个构造函数；而 [parameters.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp) 还有 `Sparam(Tparam)`（第 5-11 行）、`Tparam(S)`（第 88-98 行）、`swapPorts`（第 58-62 行）、`reduceTo`（第 64-73 行）、全部 `Yparam` 转换（第 169-184 行）以及 ABCD/T 的 `inverse()`/`root()` 均**无覆盖**——这正是 4.4 实践的选题依据。

### 4.3 校准与其余用例：假数据工厂、边界值与两个「反面教材」

#### 4.3.1 概念说明

参数转换是「纯函数」，期望值好办。但校准、去嵌入这些模块的输入是**一整条扫描的测量序列**——测试需要「无中生有」地造出这些数据。这就引出两种新范式：

- **假数据工厂**：在测试里按已知规律（线性栅格、对数栅格、带已知延迟的传输线）生成测量点，喂给被测逻辑，断言它「提取/检测」出的参数与造数据时埋进去的参数一致；
- **解析边界值**：挑选物理上有**精确解**的输入（50Ω 负载、理想开路/短路），使期望值可以心算、甚至可以用 `==` 精确比较。

同时本节也会指出仓库里的两处「反面教材」：一个**空占位用例**和一个**循环验证**用例——读测试与分析测试一样重要，能识别无效的绿色才知道什么是有效的绿色。

#### 4.3.2 核心流程

以 calibrationtests 为例的假数据工厂流程：

```
new Calibration + getKit().setIdealDefault()      ← 理想校准件，免除标准件建模
  → new Open/Short/Load 测量对象（port=1）
  → for 每个频点：构造 VNAMeasurement{frequency=f, S11=0}
       依次 addPoint 给三个测量对象
  → 调被测的栅格检测 hasFrequencyOverlap(...)
  → 断言检出的 start/stop/points/log 与造数据时的参数一致
```

关键洞察：**被测函数只看频率轴**，所以 `S11=0.0` 这种毫无物理意义的复数值也完全够用——假数据只需要在「被测函数关心的维度」上真实。

#### 4.3.3 源码精读

**校准栅格检测：三个孪生用例**

[calibrationtests.cpp:7-47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp#L7-L47) 的 `LinearDetection`：第 16-24 行造出 100 kHz–6 GHz、1001 点的理想校准件测量对象；[第 26-34 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp#L26-L34) 按线性公式 \( f_i = f_{start} + (f_{stop}-f_{start})\frac{i}{N-1} \) 生成频率并喂入；[第 41-46 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp#L41-L46) 断言检测函数还原出全部四个参数（含 `detectedLog == false`）。

[calibrationtests.cpp:49-89](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp#L49-L89) 的 `LogDetection` 唯一区别在 [第 69 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp#L69) 的对数栅格公式 \( f_i = f_{start}\cdot 10^{\,i\log_{10}(f_{stop}/f_{start})/(N-1)} \)，期望 `detectedLog == true`。

[calibrationtests.cpp:91-136](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp#L91-L136) 的 `MixedDetection` 最有趣：[第 110-123 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp#L110-L123) 让 Open 吃线性栅格、Short/Load 吃对数栅格，[第 135 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp#L135) 断言最终判为对数——u9-l2 讲过的「多数投票」行为在这里被固化为回归测试。

**utiltests：解析边界值的教科书**

[utiltests.cpp:82-108](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/utiltests.cpp#L82-L108) 的 `ImpedanceSparameterCalculation` 是策略⑤的范本：50Ω 负载的反射系数**精确**为 0（分子 50−50=0），所以 [第 87 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/utiltests.cpp#L87) 敢用 `QVERIFY(S == 0.0)` 精确比较；0Ω（短路）精确得 −1（[第 92 行](https://github.com/jankae/LibreVNA/blob/c427df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/utiltests.cpp#L92)）；100Ω 得精确的 1/3（[第 97 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/utiltests.cpp#L97)）。[第 102-107 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/utiltests.cpp#L102-L107) 还专门测了 S=1.0（对应无穷大阻抗）这个奇异点往返。

同文件的 [第 47-65 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/utiltests.cpp#L47-L65) `NoisyCircleApproximation` 展示了**噪声数据的容差带断言**：造圆时叠加模长 0.1 的随机扰动，断言就不能再用 `qFuzzyCompare`，而是 `abs(中心偏差) <= 0.1` 的**绝对误差带**——被测函数是拟合算法，本来就只承诺近似解。

[utiltests.cpp:67-80](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/utiltests.cpp#L67-L80) 的 `FirmwareComparison` 则是**表驱动**的离散值测试：九行断言覆盖「大于/等于/小于」与「位数不齐（2.2.2 对 2.3）」各分支，最后一行 `qFuzzyIsNull` 都不需要——布尔结果精确可比。

**impedancerenormalizationtests：一段注释就是一道物理题**

[impedancerenormalizationtests.cpp:18-43](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/impedancerenormalizationtests.cpp#L18-L43)：构造三个单端口「测量」（S11 = −1 短路 / 0 匹配 / 接近 1 开路），经 75Ω 再归一化后，[第 38-39 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/impedancerenormalizationtests.cpp#L38-L39) 断言 50Ω 匹配负载变成 −0.2：

\[ \Gamma' = \frac{Z_L - Z_0'}{Z_L + Z_0'} = \frac{50 - 75}{50 + 75} = -0.2 \]

短路/开路是「与参考阻抗无关」的物理不变量，所以变换后仍是 ±1（第 37、41 行）。[第 30 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/impedancerenormalizationtests.cpp#L30) 的注释「用精确的 1.0 会撞上 inf」记录了一个只有踩过坑才会写下的实现细节。

**portextensiontests：合成数据 + 一个循环验证风险**

[portextensiontests.cpp:8-25](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/portextensiontests.cpp#L8-L25) 在**构造函数**里造 501 点假数据：端口 1 是纯开路（S11=1.0），端口 2 用 [第 22 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/portextensiontests.cpp#L22) 的 `Util::addTransmissionLine(0.5, 50.0, 1e-9, 10, f)` 合成「1ns 单向延迟 + 已知损耗」的反射。`autocalc` 用例（[第 27-54 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/portextensiontests.cpp#L27-L54)）断言 PortExtension 的自动提取还原出埋进去的 1ns 与 −10·log10(0.5) dB；`correct` 用例（[第 56-70 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/portextensiontests.cpp#L56-L70)）断言修正后 S22 回到纯实数 1.0。

注意这里的**循环验证风险**：造数据的 `addTransmissionLine` 与被测的修正逻辑很可能共享同一套传输线公式——若公式符号错，两者一起错，测试照绿。它不如 `S2ABCD` 的高精度外部锚点可信，但胜在能覆盖「自动提取」这条多步骤流程。这是策略表之外值得单列的认知：**合成数据测试验证的是流程贯通，未必是公式正确**。

**ffttests 与那个空用例**

[ffttests.cpp:24-30](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/ffttests.cpp#L24-L30) 用 5 点数据（非 2 的幂，走 Bluestein 分支）对照硬编码期望值；[第 32-42 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/ffttests.cpp#L32-L42) 的往返用例在逆变换后逐点除以 `data.size()`——这两点 u8-l6 已详解。真正要新增的是本讲视角的观察：[ffttests.cpp:56-59](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/ffttests.cpp#L56-L59) 的 `fftAndIfftWithShift` 是一个**空函数体**——Qt Test 照样把它当用例执行并报 PASS。**空用例 = 占位符 = 假覆盖**，统计「全绿」时必须把它剔除。这提醒你：看测试报告先看有没有空洞，再 celebratе。

#### 4.3.4 代码实践

**实践 C：预测一个未写的用例**

1. **目标**：在不动代码的前提下，用「假数据工厂」思维预测被测行为，训练测试设计能力。
2. **步骤**：
   - 读 [calibrationtests.cpp:91-136](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp#L91-L136) 的 `MixedDetection`（1 线性 + 2 对数 → 判对数）；
   - 在纸上写出它的镜像用例 `MixedDetection2`：**2 个线性 + 1 个对数**（把第 120-122 行的喂食对象互换），先预测 `detectedLog` 的值；
   - 再进一步预测：如果三个测量的频率范围**不完全重叠**（比如 Open 只测 1–3 GHz，其余测全带），`hasFrequencyOverlap` 会返回交集还是失败？
3. **观察现象**：把两个预测写下来。
4. **预期结果**：按「多数投票」逻辑，2 线性 + 1 对数应判 `detectedLog == false`；频率不重叠的行为需要读 [calibration.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp) 中 `hasFrequencyOverlap` 的实现才能确认（u9-l2 讲过它取交集）。两个预测均**待本地验证**——验证方式就是把它写成真用例跑一遍，这正是 4.4 要教的方法。
5. 若想立即验证而不改仓库文件，可以把预测记在笔记里，等学完 4.4 后作为加练。

#### 4.3.5 小练习与答案

**练习 1**：为什么 calibrationtests 敢给所有测量点填 `S11 = 0.0` 这种物理上不存在的值？
**答案**：被测函数 `hasFrequencyOverlap` 只消费频率轴，不读 S 参数数值。假数据只需在被测函数关心的维度上真实——这也是设计假数据工厂的总原则：先问「被测函数读什么」，再造什么。

**练习 2**：`NoisyCircleApproximation` 为什么改用 `abs(偏差) <= 0.1` 而不是 `qFuzzyCompare`？
**答案**：被测的 `findCenterOfCircle` 是对带噪声数据做拟合的算法，输出本来就是近似值；`qFuzzyCompare` 的相对误差语义（且分母含 min）不适合「允许固定绝对偏差」的表达。绝对误差带才是与算法承诺匹配的断言。

**练习 3**：找出本节两个「无效绿色」的例子并各给一句修复建议。
**答案**：① `ffttests.cpp:56-59` 的空用例——要么实现（造数据 → shift → 正逆变换 → 反 shift → 比对），要么删除，别留占位；② `portextensiontests` 的循环验证——保留（它测流程贯通），但应再补一个用**外部工具**（如 scikit-rf 对同一延迟算出的相位）做锚点的用例，使公式正确性也有独立证据。

### 4.4 新增用例方法：为 S↔T 转换补一个测试

#### 4.4.1 概念说明

学测试最终是为了**写**测试。本节把新增用例的完整流程走一遍，选题是 [parameters.cpp:88-98](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L88-L98) 的 `Tparam(const Sparam&)` 与 [parameters.cpp:5-11](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L5-L11) 的 `Sparam(const Tparam&)`——它们被 u9-5 的 2x-Thru 去嵌入真实使用，却完全没有测试覆盖。

**新增用例四步清单**（写进已存在的测试文件时）：

1. 在对应 `*tests.h` 的 `private slots:` 里声明函数；
2. 在 `*tests.cpp` 里实现：构造输入 → 调被测函数 → 断言；
3. `.pro` **不用改**（文件已在 SOURCES 里）；
4. `make` 增量编译，用 `类名::函数名` 精准重跑新用例。

若要**新建**测试文件，才需要额外做三件事：新文件加进 `.pro` 的 SOURCES/HEADERS、`main.cpp` 加一行 `#include` 与一行 `qExec`、仿照现有类建 `*tests.h` 骨架。

#### 4.4.2 核心流程

```
选题（未被覆盖的纯函数，输入小到能手算）
  → 手算期望值（纸面推导，独立于实现）
  → 写用例（声明 slot + 实现 + 选对断言宏）
  → make → ./LibreVNA-Test ParameterTests::S2T
  → 失败则诊断：是手算错还是实现错？（测试第一个功劳往往是发现手算错）
  → 通过后做风格对比（QVERIFY vs QCOMPARE、float 截断、注释密度）
```

#### 4.4.3 源码精读

**被测公式：S → T**

[parameters.cpp:88-98](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L88-L98) 按如下映射把散射参数折叠成传输参数（[第 91 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L91) 还有个把「ABCD」写进错误消息的复制粘贴笔误，可作顺手修 bug 的候选——但本讲不修改源码）：

\[ T = \begin{pmatrix} -\dfrac{S_{11}S_{22}-S_{12}S_{21}}{S_{21}} & \dfrac{S_{11}}{S_{21}} \\[8pt] -\dfrac{S_{22}}{S_{21}} & \dfrac{1}{S_{21}} \end{pmatrix} \]

逆映射见 [parameters.cpp:5-11](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L5-L11)：\( S_{11}=T_{12}/T_{22},\; S_{21}=1/T_{22},\; S_{12}=(T_{11}T_{22}-T_{12}T_{21})/T_{22},\; S_{22}=-T_{21}/T_{22} \)。

**手算期望值**：为了让纸面推导可行，选**全实数**的 S 矩阵（实数运算是复数的子集，不影响覆盖；复数路径已被现有 ABCD/Z 用例覆盖）：

\[ S = \begin{pmatrix} 0.1 & 0.3 \\ 0.5 & 0.2 \end{pmatrix} \]

代入公式（分母 \(S_{21}=0.5\)）：

- \( T_{11} = -(0.1\times0.2 - 0.3\times0.5)/0.5 = -(-0.13)/0.5 = 0.26 \)
- \( T_{12} = 0.1/0.5 = 0.2 \)
- \( T_{21} = -0.2/0.5 = -0.4 \)
- \( T_{22} = 1/0.5 = 2.0 \)

**断言宏的选择**（2.3 节原则的直接应用）：

- 实部期望值 0.26/0.2/−0.4/2.0 均非零且非精确可表示 → 用 `qFuzzyCompare((float)a, 0.26f)`（float 截断 + 宽容差，仿 [parametertests.cpp:79-81](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L79-L81) 的既有注释与做法）；
- 虚部理论上为 0，实际是复数运算残留的 ~1e-17 → **必须**用 `qFuzzyIsNull((float)...)`，用 `qFuzzyCompare(x, 0.0f)` 会因 min=0 恒失败（[portextensiontests.cpp:52-53](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/portextensiontests.cpp#L52-L53) 正是这么用的）。

#### 4.4.4 代码实践

**实践 D（本讲主实践）：写出 S2T / T2S 并跑通**

1. **目标**：完整走一遍「手算 → 写用例 → 增量编译 → 精准重跑 → 风格对比」，产出两个通过的新用例。

2. **步骤**：

   第一步，在 [parametertests.h:12-19](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.h#L12-L19) 的 `private slots:` 末尾追加两行声明：

   ```cpp
   void S2T();
   void T2S();
   ```

   第二步，在 `parametertests.cpp` 末尾追加实现（**示例代码，非仓库原有内容**；遵循现有用例的命名与断言风格）：

   ```cpp
   void ParameterTests::S2T()
   {
       // 全实数矩阵，便于手算期望值；复数路径已由 S2ABCD 等用例覆盖
       auto S = Sparam(0.1, 0.3, 0.5, 0.2);
       auto t = Tparam(S);

       // 手算：T11 = -(S11*S22 - S12*S21)/S21 = -(0.02-0.15)/0.5 = 0.26
       QVERIFY(qFuzzyCompare((float)t.get(1,1).real(), 0.26f));
       QVERIFY(qFuzzyIsNull((float)t.get(1,1).imag()));
       // T12 = S11/S21 = 0.2
       QVERIFY(qFuzzyCompare((float)t.get(1,2).real(), 0.2f));
       QVERIFY(qFuzzyIsNull((float)t.get(1,2).imag()));
       // T21 = -S22/S21 = -0.4
       QVERIFY(qFuzzyCompare((float)t.get(2,1).real(), -0.4f));
       QVERIFY(qFuzzyIsNull((float)t.get(2,1).imag()));
       // T22 = 1/S21 = 2.0
       QVERIFY(qFuzzyCompare((float)t.get(2,2).real(), 2.0f));
       QVERIFY(qFuzzyIsNull((float)t.get(2,2).imag()));
   }

   void ParameterTests::T2S()
   {
       // 用 S2T 手算得到的 T 矩阵作为输入，期望值即最初的 S
       auto t = Tparam(0.26, 0.2, -0.4, 2.0);
       auto s = Sparam(t);

       QVERIFY(qFuzzyCompare((float)s.get(1,1).real(), 0.1f));
       QVERIFY(qFuzzyIsNull((float)s.get(1,1).imag()));
       QVERIFY(qFuzzyCompare((float)s.get(1,2).real(), 0.3f));
       QVERIFY(qFuzzyIsNull((float)s.get(1,2).imag()));
       QVERIFY(qFuzzyCompare((float)s.get(2,1).real(), 0.5f));
       QVERIFY(qFuzzyIsNull((float)s.get(2,1).imag()));
       QVERIFY(qFuzzyCompare((float)s.get(2,2).real(), 0.2f));
       QVERIFY(qFuzzyIsNull((float)s.get(2,2).imag()));
   }
   ```

   第三步，增量编译并精准重跑（不需要改 `.pro`，文件本就在 [LibreVNA-Test.pro:168](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/LibreVNA-Test.pro#L168) 的 SOURCES 里）：

   ```bash
   make -j$(nproc)
   ./LibreVNA-Test ParameterTests::S2T
   ./LibreVNA-Test ParameterTests::T2S
   ./LibreVNA-Test ParameterTests        # 整类回归，确认没破坏别的
   ```

3. **观察现象**：两条新用例各自输出 `PASS`；整类运行时用例总数从 6 变 8；`T2S` 的通过同时说明 4.4.3 的手算与实现互证成功。

4. **预期结果**：全部 PASS。**若 `S2T` 失败**，优先怀疑两处：手算时 \(S_{11}S_{22}-S_{12}S_{21}\) 的符号（这是最常见的笔误），或断言误用了 `qFuzzyCompare(x, 0.0f)`（见 2.3 节，与 0 比较必用 `qFuzzyIsNull`）。**若 `T2S` 失败而 `S2T` 通过**，则说明两个转换函数之一有 bug——这恰恰是测试的价值时刻，请把失败值打印出来与手算对照（可临时用 `qDebug() << t.get(1,1);`）。完整运行结果**待本地验证**。

5. **风格对比**（本实践的收尾产出，写 5-10 行笔记即可）：
   - 现有用例几乎全用 `QVERIFY`，失败时只报行号不报值；你可以在自己的用例里试把实部断言换成 `QCOMPARE((float)t.get(1,1).real(), 0.26f)`，再故意改错期望值跑一次，**体验两者失败输出的信息量差别**——`QCOMPARE` 会同时打印 actual 与 expected；
   - 现有用例把期望值集中在函数开头（如 [parametertests.cpp:23-26](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L23-L26)），而我们的示例把推导写在每条断言旁的注释里——前者紧凑，后者可维护（改一个值不用跨屏找）；两种都符合仓库整体风格，取其一并保持一致；
   - 现有用例不使用 `init()/cleanup()` fixture、不使用 `QTest::addColumn` 数据驱动；单个手算用例没必要引入，但若你要给 `reduceTo` 写「多组端口组合」的测试，数据驱动是更优雅的选项。

#### 4.4.5 小练习与答案

**练习 1**：为 `Sparam::swapPorts`（[parameters.cpp:58-62](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L58-L62)）写用例，期望值是什么？
**答案**：交换端口 1、2 后，矩阵行列同时互换：新 S11=旧 S22、新 S12=旧 S21、新 S21=旧 S12、新 S22=旧 S11。用 `Sparam(0.1, 0.3, 0.5, 0.2)` 调 `swapPorts(1,2)` 后应为 `[[0.2, 0.5], [0.3, 0.1]]`。行列**同时**换是关键（只换行列之一会得到转置而非换端口），这正是该函数容易写错、值得测的点。

**练习 2**：为 `Sparam::reduceTo`（[parameters.cpp:64-73](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L64-L73)）设计最小测试数据。
**答案**：造一个 4 端口 S 参数，全部 16 个元素填可辨识的值（如 `S.get(i,j)` 填 `i/10+j/100`），然后 `reduceTo({1,3})`，断言结果 2×2 矩阵的四个元素分别等于原 S11、S13、S31、S33（头文件 [parameters.h:56-61](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.h#L56-L61) 的注释就是这个例子，可直接当规格）。注意 `set` 是 1 基索引。

**练习 3**：为什么 `T2S` 用例不像 `S2ABCD/ABCD2S` 那样用 17 位高精度常数？
**答案**：高精度常数需要一个独立的外部计算源（当时大概来自参考软件）；我们只有纸面手算，只能得到 2-3 位有效数字，因此断言必须配合 float 截断 + 模糊比较。若想升级可信度，可以用 Python 复数运算把四个 T 值算到 17 位再硬编码——这就完全复刻了 `S2ABCD` 的锚点策略。

## 5. 综合实践

**任务：给 parameters.cpp 做一次「覆盖审计 + 补测」，产出你的第一份测试报告。**

把本讲所有环节串成一次完整的工作流（在实践 A、D 的基础上继续）：

1. **建基线**（实践 A 的产物）：`./LibreVNA-Test 2>&1 | tee test-baseline.txt`，记录 6 类 21 个非空用例的通过情况，剔除 `fftAndIfftWithShift` 这个空占位后统计**真实**覆盖。
2. **覆盖审计**：对照 [parameters.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.h) 列出全部公开构造函数与成员函数，逐一标记「已测 / 未测」，未测项再标注「是否纯函数、能否手算」。你应当至少找到：S↔T（实践 D 已补）、`swapPorts`、`reduceTo`、三个 `Yparam` 构造、`ABCDparam::operator*`/`inverse()`/`root()`、`Tparam` 的同名运算。
3. **补测**：从审计表里**再挑一个**纯函数补上用例（推荐 `swapPorts` 或 `reduceTo`，手算最简单；`ABCDparam::inverse()` 可用「乘以逆 = 单位阵」的往返策略）。
4. **验证**：`make` 后先跑新用例，再跑 `./LibreVNA-Test` 全量，与基线文件 `diff test-baseline.txt` 对比，确认**只有增加、没有破坏**。
5. **报告**（半页即可）：覆盖审计表、新增用例清单、一次「失败→诊断→修复」的记录（哪怕失败原因是自己手算错了——那也是测试在正确地工作）、以及三条你与现有用例的风格差异结论。

完成这份报告，你就具备了在本仓库「安全地改数学代码」的完整能力闭环：**改之前能跑基线，改之后能证明没坏，新功能能配上可信的测试**。

## 6. 本讲小结

- LibreVNA-Test 是**把整个 GUI 编进来的测试二进制**：`main.cpp` 一个 `QApplication` 串联 6 个 `qExec`、退出码按位或；`CONFIG += testcase` 提供 `make check`；测试文件已在 `.pro` 中意味着**在既有文件里加用例零配置**。
- 期望值来源五策略：**手算常数**（最可信）、**对偶往返**（免外部值）、**公式镜像**（须与实现走不同路径防同错）、**参数扫描**（暴露量纲错）、**解析边界值**（可精确比较）；好的套件组合使用。
- 浮点断言三定律：与 0 比较用 `qFuzzyIsNull`；多步运算的期望值用 **float 截断**放宽容差；物理精确解（50Ω→0）才可用 `==`。
- 假数据工厂原则：先问「被测函数读什么维度」，只在该维度上造真实数据（栅格检测只看频率轴，`S11=0` 即可）。
- 两处「无效绿色」警示：**空占位用例**（`fftAndIfftWithShift`）照报 PASS；**循环验证**（portextension 用被测系统自己的传输线公式造数据）只能证明流程贯通、不能证明公式正确。
- 新增用例四步：`private slots` 声明 → 实现手算期望值 → 免改 `.pro` → `类名::函数名` 精准重跑；本讲已为 S↔T 转换补出 `S2T`/`T2S` 两个通过用例（含 0.26/0.2/−0.4/2.0 的完整纸面推导）。

## 7. 下一步学习建议

1. **u11-l2 工具箱**：本讲的被测对象 `parameters.cpp` 将在下一讲以「使用者」视角重新登场——混合模式转换、阻抗匹配对话框如何消费这些矩阵运算；两讲对照着读，你会同时看清一个数学库的**内与外**。
2. **把覆盖审计扩展到 Calibration**：`calibration.cpp` 里 `computeOSL`、`correctMeasurement`（u9-2 精读过）目前只有栅格检测被测、求解器本身无单元测试。试着为「理想校准件 + 理想误差模型」构造一个可手算的闭环用例——这是仓库公认最有价值也最难的补测方向。
3. **修一个小笔误练手**：[parameters.cpp:91](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L91) 的异常消息把「T parameter」误写成「ABCD parameter」。先写一个触发该异常的测试（`QVERIFY_THROWN` 风格或 try/catch），再修消息——体验「测试先行」的微缩流程。
4. **毕业实战预告**：u11-l3 将带你实现一个完整的 `DemoDriver`。届时你写的每一行驱动代码，都可以用本讲的方法配上「已知答案」用例——测试能力是二次开发的隐形地基。
