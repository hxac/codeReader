# 测试体系：如何验证 aria2 的行为

## 1. 本讲目标

读完本讲，你应该能够：

- 说出 aria2 测试程序的完整装配链路：`configure.ac` 探测 cppunit → `test/Makefile.am` 组织源码 → `test/AllTest.cc` 充当 main 函数，把所有注册进 CppUnit 注册表的测试套件一次性跑起来。
- 拿到任意一个子系统（比如 DHT、Metalink、RPC），能在 `test/` 目录下立刻定位它对应的测试文件，并说出它受哪个编译期开关控制。
- 掌握 aria2 单元测试的标准写法：测试类的「宏三件套」、`setUp`/`tearDown`、断言风格、异常断言范式、测试数据的两种构造方式（固件文件与内存构造）。
- 学会用 `make check` 跑全量测试，并掌握「只跑一个测试套件」「用 gdb 单步跟进测试」的调试方法。
- 建立把测试用例当作「可执行的需求说明书」的习惯：读源码拿不准行为时，先读测试。

## 2. 前置知识

本讲是工程实践单元的一讲，默认你已学过 u1-l2（构建系统）和 u9-l3（日志与异常），同时需要补充几个测试领域的通用概念：

- **单元测试框架（xUnit 家族）**：几乎所有语言都有一种「测试框架传统」：JUnit（Java）、Google Test（C++）、CppUnit（C++）。它们的核心词汇是一致的——
  - **断言（assertion）**：一条「期望成立」的语句，如 `CPPUNIT_ASSERT_EQUAL(期望值, 实际值)`。断言失败即测试失败。
  - **测试用例（test case / test method）**：一个函数，验证一个行为点。
  - **测试夹具（test fixture）**：一组测试共享的准备与清理逻辑，aria2 中就是一个继承 `CppUnit::TestFixture` 的类，配 `setUp()`/`tearDown()` 两个钩子。
  - **测试套件（test suite）**：一个夹具类里全部用例的集合，或者多个套件的嵌套集合。
- **CppUnit**：xUnit 传统在 C++ 里的老牌实现，aria2 要求版本 ≥ 1.10.2。它最特别的机制是**静态注册表**：每个测试类通过一个宏把自己「登记」进进程内的一张全局表，测试 main 函数只需把这张表整体交给 Runner，而不需要手工罗列所有测试类——这一点与 Google Test 的 `RUN_ALL_TESTS()` 异曲同工。
- **测试替身（test double）**：真实依赖不可控（比如随机数、网络、另一个模块）时，用一个「假实现」顶替，让测试结果确定可复现。aria2 的 `test/FixedNumberRandomizer.h`、`MockBtMessage.h` 等都是这类。
- **固件（fixture file）**：预先放进仓库的测试数据文件，如 `test/test.torrent`。aria2 用 automake 的 `EXTRA_DIST` 把它们随源码分发。
- **`make check`**：autotools 的标准目标——编译并运行 `TESTS` 变量列出的测试程序，收集通过/失败结果。它是 `make`/`make install` 之外的第三条标准入口，承接 u1-l2 讲过的构建流水线。

一句话建立直觉：**aria2 的测试不是独立项目，而是同一套 autotools 构建里 test/ 目录下的一个额外程序**，它直接链接产品库 `libaria2.la`，因此可以触碰所有内部头文件与符号。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [test/AllTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AllTest.cc) | 测试程序唯一的 main：初始化运行环境、从 CppUnit 注册表取出全部套件并运行 |
| [test/Makefile.am](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am) | 测试的构建清单：源码列表、条件编译块、固件文件、编译宏 |
| [configure.ac](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac) | 第 204-209 行探测 cppunit 依赖 |
| [test/Bencode2Test.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Bencode2Test.cc) | 最小测试范例：一个类、一个用例、45 行 |
| [test/BittorrentHelperTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc) | 完整范式范例：setUp、48 个用例、固件与内存两种数据构造、异常断言 |
| [test/AbstractCommandTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AbstractCommandTest.cc) | 纯函数级测试范例：不需要任何夹具状态 |
| [test/Aria2ApiTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Aria2ApiTest.cc) | 集成级测试范例：整套 libaria2 会话作为夹具 |
| [test/TestUtil.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/TestUtil.h) | 测试公共工具：建文件、读文件、十六进制转字节等 |
| [test/FixedNumberRandomizer.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/FixedNumberRandomizer.h) | 测试替身范例：永远返回固定值的 Randomizer |
| [build_test.sh](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/build_test.sh) | CI 风格的构建验证脚本：15 种特性组合下 `make check` |
| [src/bittorrent_helper.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc) | 综合实践的被测对象：url-list 解析链路（承接 u6-l1） |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**测试框架组织**（测试程序如何被装配起来）、**单测编写范式**（一个测试类长什么样、数据怎么造）、**调试运行方法**（怎么构建、怎么只跑关心的用例、怎么配合 gdb）。

### 4.1 测试框架组织：AllTest.cc 与注册表装配

#### 4.1.1 概念说明

aria2 有 200 多个测试文件、上千个测试用例，但只有一个 main 函数。之所以能做到，靠的是 CppUnit 的**静态注册表**：每个测试类在自己的 .cc 文件末尾写一行 `CPPUNIT_TEST_SUITE_REGISTRATION(类名)`，这行宏会生成一个静态对象，在程序启动时（`main` 之前）把自己所属的套件塞进进程级的 `TestFactoryRegistry`。于是 `AllTest.cc` 的 main 不需要 `#include` 任何测试类，只需要：

1. 准备好运行环境（平台初始化、网络限制、输出目录、日志）；
2. 从注册表把「所有套件」做成一棵测试树；
3. 交给 `TestRunner` 运行，输出结果。

这带来一个重要的工程性质：**新增一个测试文件，通常不需要改 AllTest.cc，只需要把它加进 `test/Makefile.am` 的源码列表并注册**——装配是声明式的。

#### 4.1.2 核心流程

一次 `make check` 的完整链路：

```text
configure（探测 cppunit ≥ 1.10.2，生成 CPPUNIT_CFLAGS/CPPUNIT_LIBS）
        │
        ▼
make -C src        ──► 先产出 libaria2.la（测试链接的是产品库本体）
        │
        ▼
make -C test aria2c（测试程序也叫 aria2c，与产品二进制同名不同目录）
        │            源码 = AllTest.cc + 各 *Test.cc（按特性开关裁剪）
        │            链接 = ../src/libaria2.la + 各第三方库 + CPPUNIT_LIBS
        ▼
autotools 驱动运行 ./aria2c（整个测试树一次跑完）
        │
        ▼
结果写入 test/aria2c.log（build_test.sh 就是抓这个文件归档的）
```

测试程序内部（AllTest.cc 的 main）：

```text
initConsole → Platform 构造 → 限制 IPv4 / 清 AI_ADDRCONFIG
→ mkdirs(test_outdir) → 控制台日志调到 DEBUG
→ registry.makeTest()（整棵测试树）
→ TestRunner.run()（不接收命令行参数 → 开箱即用只能全量跑）
→ 返回 0/1 作为退出码
```

#### 4.1.3 源码精读

**① 依赖探测**：configure.ac 用 pkg-config 找 cppunit，找不到只警告不中止：

[configure.ac:204-209](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L204-L209)

这段代码通过 `PKG_CHECK_MODULES` 探测 `cppunit >= 1.10.2`，成功则填充 `CPPUNIT_CFLAGS`/`CPPUNIT_LIBS` 两个替换变量，失败则把 `have_cppunit` 置 no 并只发警告——也就是说 configure 不会因此失败，缺库的后果要到 `make check` 链接测试程序时才暴露（待本地验证）。

**② 测试 main 的环境准备**：

[test/AllTest.cc:16-40](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AllTest.cc#L16-L40)

这段代码做了六件事，每一件都值得注意：

- 第 18-19 行：`initConsole` 与构造 `Platform`——和真实 `aria2c` 的启动（u1-l3）完全同款，`Platform` 负责 OpenSSL、c-ares 等第三方库的一次性初始化，没有它，任何用到 TLS/DNS 的测试都跑不起来。
- 第 32 行：`SocketCore::setProtocolFamily(AF_INET)`——把测试统一限制在 IPv4。源码注释解释了原因：`bind()` 与 `establishConnection()` 若解析到不同协议族，后者连不上前者。
- 第 35 行：`setDefaultAIFlags(0)`——清掉 `AI_ADDRCONFIG`，避免「机器没配 IPv4 地址时测试集体失败」。
- 第 37 行：创建输出目录 `A2_TEST_OUT_DIR`（编译期宏，值是相对路径 `test_outdir`），部分写文件的测试产物都落在这里，`make clean` 时由 Makefile 的 `clean-local` 整目录删除。
- 第 39-40 行：把控制台日志级别调到 `DEBUG`——所以测试运行时你能看到全部调试日志，这与命令行模式「控制台默认 NOTICE」（u9-l3）不同，调试时非常有用。

**③ 注册表装配**：

[test/AllTest.cc:42-52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AllTest.cc#L42-L52)

这段代码是整个测试程序的「心脏」：`TestFactoryRegistry::getRegistry().makeTest()` 返回一棵名为 "All Tests" 的测试树，树的孩子就是所有执行过 `CPPUNIT_TEST_SUITE_REGISTRATION` 的套件（包括它们的类名，如 `BittorrentHelperTest`）。`CompilerOutputter` 把失败断言格式化成「编译器错误」样式（文件:行号 + 表达式展开），与 u9-l3 讲过的日志行头风格一致。注意第 50 行 `runner.run()` **没有转发 argv**——这决定了 4.3 节要讨论的「如何只跑部分测试」。

**④ 构建清单与条件编译**：

[test/Makefile.am:1-4](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L1-L4)

这几行声明：`TESTS = aria2c`（autotools 要运行的测试程序清单）、`check_PROGRAMS`（`make check` 时才编译的程序）、以及测试程序的源码列表开头。**测试二进制与产品二进制同名**，都叫 `aria2c`，靠目录区分（`src/aria2c` 与 `test/aria2c`），这在 shell 里手敲命令时务必小心。

源码列表不是一整块，而是被十来个条件块切分，与 u1-l2 讲过的特性开关一一对应。最典型的是 BitTorrent 块：

[test/Makefile.am:120-220](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L120-L220)

这个 `if ENABLE_BITTORRENT` 块包含了全部 Bt 消息、DHT、UT 扩展的测试，`BittorrentHelperTest.cc` 在第 209 行、`Bencode2Test.cc` 在第 215 行。类似地：

- [test/Makefile.am:240-242](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L240-L242)：`if ENABLE_LIBARIA2` 才编入 `Aria2ApiTest.cc`；
- [test/Makefile.am:103-109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L103-L109)：`if HAVE_ZLIB` 才编入 gzip 相关测试；
- [test/Makefile.am:222-230](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L222-L230)：`if ENABLE_METALINK` 才编入 Metalink 测试。

**推论**：不同特性组合编译出的测试集不一样。CI 脚本 build_test.sh 正是利用这一点，用 15 种配置各跑一遍 `make check`：

[build_test.sh:28-33](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/build_test.sh#L28-L33)

这段脚本执行 `make -j2 check`，并把 `test/aria2c.log`（autotools 测试驱动的输出日志）追加归档——它证实了「`make check` + 日志文件」就是项目的标准测试入口。

**⑤ 链接与编译宏**：

[test/Makefile.am:244-246](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L244-L246)

`aria2c_LDADD` 第一项就是 `../src/libaria2.la`——测试直接链接产品库本体，所以测试代码可以 include 任何 `src/` 的内部头文件、调用未导出的符号，这是白盒测试的前提。

[test/Makefile.am:266-273](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L266-L273)

`AM_CPPFLAGS` 给所有测试源码注入两个宏：`A2_TEST_DIR` 指向源码树的 test/ 目录（定位固件文件），`A2_TEST_OUT_DIR` 指向输出目录。所以你在测试里看到的 `A2_TEST_DIR "/test.torrent"` 就是编译期拼接的固件绝对路径。

#### 4.1.4 代码实践

**实践目标**：亲手跑通一次全量测试，并验证「测试集随特性开关变化」。

**操作步骤**：

1. 按你机器上的依赖情况执行 configure（缺依赖时可用 u1-l2 讲过的 `--without-*` 开关裁剪）：

   ```bash
   autoreconf -i
   ./configure
   make -j$(nproc)
   ```

2. 跑全量测试：

   ```bash
   make check
   ```

3. 查看结果：

   ```bash
   tail -20 test/aria2c.log     # autotests 驱动日志
   ls test/test_outdir/         # 测试产物目录
   ```

4. 统计本机测试集规模，并验证条件编译：

   ```bash
   grep -c 'CPPUNIT_TEST_SUITE_REGISTRATION' test/*.cc | wc -l   # 注册的套件数
   grep -c '^  CPPUNIT_TEST(' test/BittorrentHelperTest.cc       # 该套件用例数
   ```

**需要观察的现象**：

- `make check` 末尾的 `PASS: aria2c` 或 `FAIL: aria2c` 汇总行；
- 测试运行时控制台滚动输出大量 DEBUG 级日志（这是 AllTest.cc 第 39 行的效果）；
- 若你的 configure 用了 `--disable-bittorrent`，第 4 步中 grep 不到 BittorrentHelperTest 的注册（文件根本没参与编译）。

**预期结果**：全量测试通过；`grep -c '^  CPPUNIT_TEST(' test/BittorrentHelperTest.cc` 输出 48（本讲 HEAD 下的用例数）。若某用例失败，日志中会以「编译器错误」样式给出断言所在文件与行号。整个过程在本机首次编译耗时可能较长，属正常现象（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：DHT 相关的测试（如 `DHTNodeTest.cc`）受哪个开关控制？为什么不在独立开关下？

**答案**：受 `ENABLE_BITTORRENT` 控制（[test/Makefile.am:120-220](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L120-L220) 块内，DHT 测试集中在第 168-192 行附近）。因为 aria2 的 DHT 是 BitTorrent 子系统的一部分（为 BT 找 peer 服务，u7 单元），configure.ac 没有为它单设开关，测试的分组自然跟随产品特性分组。

**练习 2**：为什么 AllTest.cc 要把协议族限制为 `AF_INET`、还要清掉 `AI_ADDRCONFIG`？

**答案**：源码注释（[test/AllTest.cc:27-35](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AllTest.cc#L27-L35)）说明了两个原因：一是测试里 `SocketCore::bind()` 和 `establishConnection()` 可能因 DNS 解析结果落在不同协议族（一个 IPv4 一个 IPv6）而连不上；二是设置了 `AI_ADDRCONFIG` 时，若机器没配 IPv4 地址，名字解析会直接失败。两条都是为了消除环境差异，保证测试在任意机器上行为一致。

**练习 3**：测试程序和产品程序都叫 `aria2c`，如何避免误运行？

**答案**：靠路径区分——产品在 `src/aria2c`，测试在 `test/aria2c`；autotools 的 `make check` 会在 test 目录下自动运行正确的那个。手工运行时建议始终带目录前缀 `./test/aria2c`。

### 4.2 单测编写范式：一个测试类的解剖

#### 4.2.1 概念说明

aria2 的测试没有用任何花哨框架设施，就是一个个普通的 C++ 类。但九百多个源码文件对应的测试写法高度统一，掌握这一个范式就能读懂全部测试。范式由五要素组成：

1. **类声明**：继承 `CppUnit::TestFixture`，成员变量充当夹具状态；
2. **宏三件套**：`CPPUNIT_TEST_SUITE(类名)` 开清单 → 每个用例一行 `CPPUNIT_TEST(方法名)` → `CPPUNIT_TEST_SUITE_END()` 关清单；
3. **注册**：文件末尾 `CPPUNIT_TEST_SUITE_REGISTRATION(类名)`，挂入全局注册表；
4. **setUp/tearDown**：每个用例运行前后各执行一次，负责构造/销毁夹具；
5. **断言**：以 `CPPUNIT_ASSERT_EQUAL(期望, 实际)` 为主。

测试数据的构造有两条路线：**固件文件**（把 `.torrent`、`.xml`、`.netrc` 等样本直接放进仓库，用 `A2_TEST_DIR` 定位）与**内存构造**（用 ValueBase/bencode 在代码里现搭数据结构）。前者适合复杂真实样本，后者适合精确控制边界条件（如非法输入、非 UTF-8 路径）。

#### 4.2.2 核心流程

一个测试用例的执行流程：

```text
Runner 取出套件 BittorrentHelperTest
  │ 对清单里的每个用例（按 CPPUNIT_TEST 登记顺序）：
  ├─► setUp()                构造 Option、设置 PREF_DIR
  ├─► testXxx()              执行断言；抛 CPPUNIT 异言失败即记录
  └─► tearDown()（如重写）   清理
全部套件跑完 → CompilerOutputter 汇总输出 → 进程退出码 0/1
```

两条数据构造路线的对比：

| 维度 | 固件文件路线 | 内存构造路线 |
| --- | --- | --- |
| 数据在哪 | `test/*.torrent` 等仓库内文件 | 测试代码里的 ValueBase 字典 / 手写 bencode 串 |
| 定位方式 | `A2_TEST_DIR "/xxx.torrent"` 编译期拼路径 | 直接传给 `loadFromMemory` |
| 擅长 | 复杂真实样本、回归样本 | 边界条件、非法输入、最小可复现 |
| 修改成本 | 需重新生成二进制文件（通常用脚本） | 改几行代码即可 |

#### 4.2.3 源码精读

**① 最小完整范例——Bencode2Test**：

[test/Bencode2Test.cc:9-20](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Bencode2Test.cc#L9-L20)

这 12 行就是范式的全部骨架：类 `Bencode2Test` 继承 `TestFixture`；第 11-13 行是宏三件套（本套件只登记 `testEncode` 一个用例）；第 20 行注册进全局表。注意整个文件只有 45 行——一个测试文件的最小体积不过如此。

[test/Bencode2Test.cc:22-43](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Bencode2Test.cc#L22-L43)

这个用例演示了典型的断言写法：用 `Dict`/`List`/`String::g`/`Integer::g` 现搭一棵 ValueBase 树（u9-l2 讲过的类型系统），再用 `CPPUNIT_ASSERT_EQUAL` 断言 `bencode2::encode` 的输出逐字节等于手写的期望串。注意期望值先写、实际值后写，且两侧类型必须完全一致（都是 `std::string`），这是 `CPPUNIT_ASSERT_EQUAL` 模板的硬性要求——后面你会反复看到 `(size_t)2`、`std::string("...")` 这类显式转换，都是为了满足它。

**② 完整范式——BittorrentHelperTest 的类骨架**：

[test/BittorrentHelperTest.cc:26-77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L26-L77)

这段是套件清单：第 28 行开清单、第 77 行关清单，中间 48 行每行登记一个用例。清单顺序即执行顺序。被测对象是 u6-l1 讲过的 `bittorrent_helper`（种子解析）。

[test/BittorrentHelperTest.cc:82-86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L82-L86)

`setUp` 在**每个用例前**都会执行一遍：new 一个 `Option`，把 `PREF_DIR` 设为当前目录——因为被测的 `load()` 会依据该选项把种子里的文件路径展开成磁盘路径。这就是夹具的意义：每个用例拿到全新、干净的依赖状态，用例之间互不污染。

[test/BittorrentHelperTest.cc:138-148](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L138-L148)

第 138 行注册套件；`testGetInfoHash` 则是最典型的「固件文件路线」用例：加载 `test/test.torrent`，断言 infohash 等于手算出的 SHA-1 十六进制串。这个期望值同时是一份「文档」——它固定了 test.torrent 这个样本的指纹，任何人改坏解析逻辑都会在此翻车。

**③ 固件文件路线的 url-list 用例**（综合实践的模仿对象）：

[test/BittorrentHelperTest.cc:367-406](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L367-L406)

这个用例加载 `url-list-multiFile.torrent`（[test/Makefile.am:321-323](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L321-L323) 把三个 url-list 样本列入 EXTRA_DIST），验证多文件种子中 url-list 如何变成每个 FileEntry 的 HTTP 镜像 URI。注意断言里的 `(size_t)2`、`std::string(...)` 显式转换，以及从 `dctx->getFileEntries()` 一路取到 `getRemainingUris()` 的检查粒度。

**④ 内存构造路线**：

[test/BittorrentHelperTest.cc:475-490](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L475-L490)

这个用例演示内存构造：用 `Dict::g()`/`List::g()` 现搭一个种子字典，`bencode2::encode(&dict)` 序列化成字节串后交给 `loadFromMemory`。其中 `1_k` 是 aria2 自定义的字面量后缀（[src/a2functional.h:172](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/a2functional.h#L172) 定义 `operator"" _k`，`1_k` 即 1024）。这条路线的价值在此例中体现得淋漓尽致：**非 UTF-8 的文件名字节序列没法手写进 bencode 字符串，但可以 `fromHex("90a28a")` 精确拼出**。

更直接的手写 bencode 串版本见 [test/BittorrentHelperTest.cc:492-511](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L492-L511)——把整个种子文件内容当作一个字符串字面量直接传入。

**⑤ 异常断言范式**：

[test/BittorrentHelperTest.cc:513-527](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L513-L527)

要断言「必须抛异常」，aria2 统一用 `try { 执行; CPPUNIT_FAIL("exception must be thrown."); } catch (Exception& e) { /* OK */ }` 三段式：没抛异常就用 `CPPUNIT_FAIL` 主动判负，抛了则落入空 catch 视为通过。项目里没有使用 `CPPUNIT_ASSERT_THROW` 一类现成宏，新写用例时应保持同一风格。

**⑥ 纯函数级与集成级的光谱两端**：

[test/AbstractCommandTest.cc:13-27](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AbstractCommandTest.cc#L13-L27)

`AbstractCommandTest` 测的是 `getProxyUri` 这个纯函数（u4-l4 讲过的代理选择规则），不需要任何夹具状态——注意它的 `setUp`/`tearDown` 显式写了空实现，这是可选项。用例本体（[test/AbstractCommandTest.cc:29-67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AbstractCommandTest.cc#L29-L67)）就是连续十几个断言，逐行推进 Option 的设置、逐行验证取值优先级——读起来就像一份可执行的规则说明书。

[test/Aria2ApiTest.cc:17-50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Aria2ApiTest.cc#L17-L50)

另一端是 `Aria2ApiTest`：夹具是**一整套 libaria2 会话**——`setUp` 里 `sessionNew`、`tearDown` 里 `sessionFinal`（u8-l5 讲过的 C API），每个用例都在一个真实引擎上操作。它展示了两件事：一是夹具可以重到「整个下载引擎」；二是用例内如何处理条件特性——[test/Aria2ApiTest.cc:77-94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Aria2ApiTest.cc#L77-L94) 用 `#ifdef ENABLE_METALINK` 给两种编译结果各写一套期望值（metalink 开启时期望 2 个 GID，关闭时 1 个——还嵌套了 `ENABLE_BITTORRENT` 判断）。这提醒我们：**同一个测试文件在不同特性组合下跑的断言可能不同**。

**⑦ 测试替身**：

[test/FixedNumberRandomizer.h:9-26](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/FixedNumberRandomizer.h#L9-L26)

`computeFastSet`（BEP 10 的快速集合）依赖随机数，真实 Randomizer 会让结果不可复现。`FixedNumberRandomizer` 覆写 `getRandomNumber` 永远返回固定值，让随机算法变成确定算法。`test/Makefile.am` 的源码清单里还能看到一整批 `Mock*.h` 头文件（MockBtMessage、MockDHTMessageFactory、MockPieceStorage 等，见 [test/Makefile.am:142-151](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L142-L151) 与 [test/Makefile.am:196-211](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L196-L211)），都是同一思想的产物：**被测单元的协作者全部换成可控的假实现，失败时才能定位到被测单元本身**。

公共工具集中在 [test/TestUtil.h:18-46](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/TestUtil.h#L18-L46)：`createFile`（造指定长度的文件）、`readFile`、`fromHex`（十六进制串转字节串，构造非打印字符数据时高频使用）等。

#### 4.2.4 代码实践

**实践目标**：通过「阅读 + 统计」验证你对范式与两条数据路线的理解。

**操作步骤**：

1. 打开 [test/BittorrentHelperTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc)，找到 4.2.3 中列出的五要素在文件中的确切位置（类声明、宏三件套、注册、setUp、任一用例）。
2. 统计两种数据路线的使用比例：

   ```bash
   grep -c 'A2_TEST_DIR' test/BittorrentHelperTest.cc          # 固件文件路线
   grep -c 'loadFromMemory' test/BittorrentHelperTest.cc       # 内存路线（含声明）
   ls test/*.torrent | wc -l                                   # 仓库里的种子固件数
   ```

3. 挑一个从未读过的测试文件（如 `test/AnnounceListTest.cc`），只看类骨架与断言，不看实现，猜它验证的行为，再对照 u6-l1 讲过的 AnnounceList 语义核对猜测。

**需要观察的现象**：BittorrentHelperTest 中 `A2_TEST_DIR` 的出现次数明显少于 `loadFromMemory`——近期新增用例倾向内存构造；`test/` 下有十多个固件文件，其中 `load.aria2`、`load-v0001.aria2` 等还是**多版本格式样本**（u6-l11 的控制文件），专门用于回归测试旧格式兼容。

**预期结果**：能对任意 `*Test.cc` 在 30 秒内说出「被测对象、夹具构成、数据来源」三要素。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CPPUNIT_ASSERT_EQUAL` 的参数里到处是 `(size_t)2`、`std::string("...")` 这样的显式转换？

**答案**：该断言是函数模板，要求两个参数类型完全一致才能比较（否则模板推导失败，直接编译错误）。字面量 `2` 是 `int`，而 `size()` 返回 `size_t`；字符串字面量是 `const char*`，而接口返回 `std::string`——都要先转换成同一类型。

**练习 2**：`testLoadFromMemory_multiFileNonUtf8Path` 为什么要用 `fromHex("90a28a")` 而不是直接写字符串？

**答案**：`90 a2 8a` 是非法 UTF-8 序列，没法以可读字符写进源码字符串；用十六进制转义构造原始字节，可以精确注入「非 UTF-8 文件名」这个测试条件，随后断言其被百分号编码为 `%90%A2%8A`（目录穿越消毒与编码逻辑，见 u6-l1）。这正是内存构造路线相对固件文件的核心优势。

**练习 3**：一个测试类有 48 个用例，`setUp` 会被执行多少次？

**答案**：48 次。xUnit 框架的约定是每个用例独享一份新夹具（先 setUp 后用例再 tearDown），用例间不共享状态；所以夹具构造必须轻量，重夹具（如 Aria2ApiTest 的整个会话）是明知代价后的取舍。

### 4.3 调试运行方法：构建、过滤与单步

#### 4.3.1 概念说明

「测试能跑」只是第一步，高效的源码学习要求三件事：**快速重跑**（改一行测试立刻看结果）、**只跑关心的用例**（全量测试输出会淹没你关心的那个断言）、**能配合调试器**（断点停在被测代码里看真实状态）。

aria2 的特殊之处在于：AllTest.cc 的 `runner.run()` 不接收任何命令行参数，所以**开箱即用的测试二进制永远跑全量**。想过滤，要么小改一行 AllTest.cc，要么借助 gdb。另外要记住测试运行的两大便利：控制台日志已是 DEBUG 级；日志行头带 `CUID#`（u9-l3），可与命令链对上。

#### 4.3.2 核心流程

三条调试路径的决策：

```text
想验证行为 ──┬─ 全量 ──────────────► make check（最省事，先看汇总行）
             ├─ 只跑一个套件 ─────► 改 AllTest.cc 转发 argv → ./test/aria2c 套件名
             └─ 看内部状态 ───────► gdb ./test/aria2c，断点打在 src/ 的被测函数
                                        （或临时在测试里加打印）
```

#### 4.3.3 源码精读

**① 快速构建测试二进制**。automake 会为 `check_PROGRAMS` 里的每个程序生成显式构建规则，所以不必每次 `make check` 走完整仪式：

```bash
make -C test aria2c     # 只链接测试程序
./test/aria2c           # 手工运行（全量）
```

增量编译下，改一个测试文件后 `make -C test aria2c` 只重编那一个文件再链接，秒级完成（待本地验证）。注意 `make check` 与手工运行的区别：前者由 autotests 驱动、把 stdout/stderr 收进 `test/aria2c.log` 并按 `TESTS` 汇总 PASS/FAIL；后者直接打到终端。

**② 只跑一个套件——一行改造**。CppUnit 的 `TextUi::TestRunner::run` 本身支持一个 `testName` 参数（传入套件名即只跑该套件），只是 AllTest.cc 没有转发 argv。把 [test/AllTest.cc:50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AllTest.cc#L50) 这一行：

```cpp
bool successfull = runner.run();
```

临时改为：

```cpp
bool successfull = runner.run(argc > 1 ? argv[1] : "");
```

重新 `make -C test aria2c` 后即可：

```bash
./test/aria2c BittorrentHelperTest              # 只跑一个套件（待本地验证）
./test/aria2c 'BittorrentHelperTest/testGetInfoHash'   # 能否精确到单个用例、
                                                      # 路径分隔符是 / 还是 .，
                                                      # 取决于所装 cppunit 版本（待本地验证）
```

这是本地学习用的临时改动，实践完记得还原，不要提交。若不想改任何文件，替代方案是直接跑全量（终端翻回失败处即可），或用下一条路径。

**③ gdb 配合**。测试是普通程序，gdb 用法与调试 aria2c 完全一致（u9-l3 的方法在这里全部适用）：

```bash
gdb --args ./test/aria2c
(gdb) break aria2::bittorrent::loadFromMemory
(gdb) run
```

断在 src/ 的实现函数里，就能看到测试喂进去的真实数据在解析管线中如何流动。配合「内存构造路线」的用例，数据完全可预期，单步观察效率极高。也可以在测试用例里临时插入 `std::cout` 打印中间值——测试代码不是产品代码，学习期间怎么方便怎么来（同样别提交）。

**④ 把测试当实验台的方法论**。读源码拿不准某个分支时，最快的验证方式往往是「在测试里加一个用例」而不是「改命令行跑一次」：

- 命令行验证要真实网络/文件/代理等环境，慢且不可复现；
- 测试用例是确定性输入 + 确定性断言，一次写好终身回归；
- 测试名即文档，日后翻 test/ 目录就能想起当时验证过什么。

#### 4.3.4 代码实践

**实践目标**：体验「只跑一个套件」与「gdb 单步」两种调试方式。

**操作步骤**：

1. 按 4.3.3 的方法临时修改 AllTest.cc 第 50 行转发 argv，`make -C test aria2c` 重编。
2. 分别运行：

   ```bash
   ./test/aria2c Bencode2Test
   ./test/aria2c BittorrentHelperTest
   ```

3. 还原 AllTest.cc 的改动（`git checkout test/AllTest.cc`）。
4. gdb 实验：

   ```bash
   gdb --args ./test/aria2c
   (gdb) break aria2::bittorrent::extractUrlList
   (gdb) run
   # 停下后查看调用栈：bt；继续：c
   ```

**需要观察的现象**：第 2 步的输出应只包含指定套件的运行结果（如 OK 或失败断言），耗时显著短于全量；第 4 步的断点会命中——但注意 `extractUrlList` 是匿名命名空间里的函数，gdb 断点符号能否解析取决于编译信息，若断不上可改断 `aria2::bittorrent::load`（待本地验证）。

**预期结果**：两三条命令完成「改—编—跑—看」的完整循环；理解为什么 aria2 开发者能对一个 950 文件的项目保持快速迭代。

#### 4.3.5 小练习与答案

**练习 1**：为什么 aria2 的测试 main 不把 argv 转发给 `runner.run()`？

**答案**：只能推测设计意图：项目以 `make check` 全量回归为准（CI 的 build_test.sh 也是全量），全量跑完才有整体绿色/红色的结论；过滤用例属于个人调试习惯，开发者更愿意用 gdb 或临时打印。事实层面可以确定的是：[test/AllTest.cc:50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AllTest.cc#L50) 的 `runner.run()` 不带参数，因此过滤能力从未暴露给命令行。

**练习 2**：测试失败时，`CompilerOutputter` 的输出长什么样、为什么说它「像编译器错误」？

**答案**：它会以「测试源文件:行号: 断言表达式与期望/实际值」的形式打印失败点（如 `equality assertion failed - Expected: 2, Actual: 3`），与 gcc 报错「文件:行号: 消息」的格式同构，因此开发者可以直接用看编译错误的眼习惯定位失败断言所在的测试代码行。

## 5. 综合实践

**任务**：为 `BittorrentHelperTest` 补一个新用例 `testLoadFromMemory_multiFileUrlList`——用「内存构造路线」建一个带 `url-list` 的多文件种子字典，验证 url-list 被正确解析并挂到每个 FileEntry 上。这个任务把本讲三个模块全部串起来：改测试源码（4.1 的构建链路）、按范式写用例（4.2）、增量编译并验证（4.3）。

### 5.1 背景与被测行为

被测链路在 `src/bittorrent_helper.cc`（u6-l1 精读过，这里只取与 url-list 有关的四段）：

- 种子根字典的 `url-list` 键（常量 `C_URL_LIST` 定义于 [src/bittorrent_helper.cc:81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L81)）由 `extractUrlList` 提取，它用 visitor 同时兼容单字符串与字符串列表两种形态（[src/bittorrent_helper.cc:113-157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L113-L157)），结果同时记入 `TorrentAttribute::urlList`（[src/TorrentAttribute.h:61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TorrentAttribute.h#L61)）。
- 提取后先与外部 URI 合并、排序去重（[src/bittorrent_helper.cc:489-493](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L489-L493)）——所以 FileEntry 里的 URI 顺序是**字典序**，与 url-list 中的书写顺序无关。
- 多文件模式下，url-list 的每项基址与文件相对路径拼接成 FileEntry 的 URI，拼接口径见 [src/bittorrent_helper.cc:160-175](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L160-L175)：基址以 `/` 结尾直接拼，否则补一个 `/`；调用点在 [src/bittorrent_helper.cc:286-287](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L286-L287)。
- 人口 `loadFromMemory` 重载族见 [src/bittorrent_helper.h:90-107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.h#L90-L107)。

### 5.2 测试数据是如何构造的

完全仿照 [test/BittorrentHelperTest.cc:444-473](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L444-L473) 的字典搭法，三处关键设计：

1. **最小合法种子**：`info` 字典只需要 `files`（length + path）、`piece length`、`pieces`、`name` 四项。`pieces` 给 20 字节字符串 `"01234567890123456789"`（SHA-1 长度 = 1 个 piece 的哈希），配合 `length = 1_k`、`piece length = 1_k` 恰好满足「总长/piece 长度 = 哈希数」的一致性校验（[src/bittorrent_helper.cc:498-501](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L498-L501)），否则直接抛 `BITTORRENT_PARSE_ERROR`。
2. **url-list 放根字典**（与 `info` 平级，不是 info 里），且故意用「一个不带尾斜杠 + 一个带尾斜杠」两个基址，一次性覆盖 createUri 的两个分支。
3. **两个基址故意倒序书写**（mirror 在前 localhost 在后），用于验证解析后的排序去重：字典序 `http://localhost/... < http://mirror/...`，断言时按排好序的写。

### 5.3 具体操作

**第 1 步**：在 [test/BittorrentHelperTest.cc:47-49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L47-L49) 现有的三个 url-list 用例之后，向套件清单登记新用例：

```cpp
  CPPUNIT_TEST(testGetFileEntries_multiFileUrlList);
  CPPUNIT_TEST(testGetFileEntries_singleFileUrlList);
  CPPUNIT_TEST(testGetFileEntries_singleFileUrlListEndsWithSlash);
  CPPUNIT_TEST(testLoadFromMemory_multiFileUrlList);   // ← 新增这一行（示例改动）
```

**第 2 步**：在类声明的 public 方法区（[test/BittorrentHelperTest.cc:106-108](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L106-L108) 附近）加一行声明：

```cpp
  void testLoadFromMemory_multiFileUrlList();   // ← 新增（示例改动）
```

**第 3 步**：在文件实现区（建议紧跟 `testGetFileEntries_singleFileUrlListEndsWithSlash` 之后，即 [test/BittorrentHelperTest.cc:442](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L442) 之后）添加实现。以下为**示例代码**（仿照既有用例风格编写，未在本地运行）：

```cpp
void BittorrentHelperTest::testLoadFromMemory_multiFileUrlList()
{
  // 内存构造一个带 url-list 的多文件种子字典
  auto path = List::g();
  path->append("aria2");
  path->append("src");
  path->append("aria2c");
  auto file = Dict::g();
  file->put("length", Integer::g(1_k));
  file->put("path", std::move(path));
  auto files = List::g();
  files->append(std::move(file));
  auto info = Dict::g();
  info->put("files", std::move(files));
  info->put("piece length", Integer::g(1_k));
  info->put("pieces", "01234567890123456789");
  info->put("name", "aria2-test");
  // url-list 在根字典（与 info 平级）；两个基址故意倒序书写，
  // 一个带尾斜杠、一个不带，分别覆盖 createUri 的两个分支
  auto urlList = List::g();
  urlList->append(String::g("http://mirror/dist"));
  urlList->append(String::g("http://localhost/dist/"));
  Dict dict;
  dict.put("info", std::move(info));
  dict.put("url-list", std::move(urlList));

  auto dctx = std::make_shared<DownloadContext>();
  loadFromMemory(bencode2::encode(&dict), dctx, option_, "default");

  // TorrentAttribute::urlList 收录两个基址
  auto attrs = getTorrentAttrs(dctx);
  CPPUNIT_ASSERT_EQUAL((size_t)2, attrs->urlList.size());

  // 多文件模式下，基址与文件相对路径拼接为 FileEntry 的 URI，
  // 且经过排序：localhost < mirror
  auto& fe = dctx->getFirstFileEntry();
  const std::deque<std::string>& uris = fe->getRemainingUris();
  CPPUNIT_ASSERT_EQUAL((size_t)2, uris.size());
  CPPUNIT_ASSERT_EQUAL(
      std::string("http://localhost/dist/aria2/src/aria2c"), uris[0]);
  CPPUNIT_ASSERT_EQUAL(
      std::string("http://mirror/dist/aria2/src/aria2c"), uris[1]);
}
```

**第 4 步**：增量编译并验证：

```bash
make -C test aria2c    # 只重编 BittorrentHelperTest.cc 并链接（待本地验证）
make check             # 或走全量回归
```

### 5.4 需要观察的现象与预期结果

- 新用例被执行（输出里套件用例计数 +1），断言全部通过。
- 期望值推导依据：`http://mirror/dist` 不带尾斜杠 → createUri 补 `/` 再拼路径，得 `http://mirror/dist/aria2/src/aria2c`；`http://localhost/dist/` 带尾斜杠 → 直接拼，得 `http://localhost/dist/aria2/src/aria2c`；排序后 localhost 在前。路径段 `aria2/src/aria2c` 全部是 URI 安全字符，percentEncode 不改变它们（若你的种子路径含 `@` 等特殊字符，则参照 [test/BittorrentHelperTest.cc:380-392](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L380-L392) 的期望值写成 `%40` 形式）。
- 若第 4 步断言失败，先用 `std::cout << uris[0] << std::endl;` 打印实际值对照（这正是「测试当实验台」的用法）；上例代码未经本地运行，具体期望值待本地验证。

### 5.5 收尾

实践完成后 `git checkout test/BittorrentHelperTest.cc` 还原（或保留在你的学习分支）。想一想：这个用例与既有的 `testGetFileEntries_multiFileUrlList`（固件文件版）覆盖了同一被测代码，价值差异在哪？——内存版把「数据构造规则」显式写进了代码，读者不需要二进制工具就能看懂种子结构；固件版则更接近真实世界的种子。两者是互补而非重复。

## 6. 本讲小结

- aria2 的测试是 autotools 体系里 test/ 目录下的一个**与产品二进制同名的程序** `test/aria2c`：它直接链接 `libaria2.la`，可白盒触及一切内部符号；由 `make check` 驱动，结果落在 `test/aria2c.log`。
- 装配是**声明式**的：每个测试类用 `CPPUNIT_TEST_SUITE_REGISTRATION` 静态挂入 CppUnit 注册表，[test/AllTest.cc:42-50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AllTest.cc#L42-L50) 只负责准备环境（Platform、限 IPv4、DEBUG 日志）并整体运行；新增测试一般只需「一个文件 + Makefile.am 一行」。
- 测试集随特性开关**裁剪**：ENABLE_BITTORRENT/ENABLE_METALINK/ENABLE_LIBARIA2/HAVE_ZLIB 等条件块决定哪些 `*Test.cc` 参与编译，不同构建产物的测试面不同，CI 用 15 种组合各跑一遍 `make check`。
- 单测写法五要素高度统一：TestFixture 类、宏三件套、注册、setUp/tearDown、`CPPUNIT_ASSERT_EQUAL`（注意两侧类型必须严格一致）；异常断言用 `try/CPPUNIT_FAIL/catch` 三段式。
- 测试数据两条路线：**固件文件**（`A2_TEST_DIR` 定位，适合真实样本与多版本格式回归）与**内存构造**（ValueBase 字典 + `bencode2::encode` + `loadFromMemory`，适合边界与非法输入）；不可控依赖用 `FixedNumberRandomizer`、`Mock*.h` 等测试替身替换。
- 调试三板斧：`make -C test aria2c` 增量重编；临时改 [test/AllTest.cc:50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AllTest.cc#L50) 转发 argv 以按套件名过滤（原生不支持，属本地学习改动）；`gdb --args ./test/aria2c` 配合 DEBUG 级控制台日志单步观察。

## 7. 下一步学习建议

- **下一讲 u9-l6（二次开发实战）**：本讲的「新增一个测试用例」是热身，下一讲把范围扩大到「新增一个选项 + 新行为 + 配套测试」的完整功能闭环，请带着综合实践的手感进入。
- **建议继续阅读的源码**：
  - [test/TestUtil.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/TestUtil.cc)：看公共夹具工具如何隔离文件系统副作用；
  - [test/DefaultBtMessageDispatcherTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DefaultBtMessageDispatcherTest.cc)：观察「多 Mock 协作」的大型夹具如何组织（配合 u6-l5）；
  - [test/RequestGroupTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/RequestGroupTest.cc)：任务层行为的可执行规格（配合 u3-l1）。
- **一个长期习惯**：此后每学一个子系统，先读它的 `*Test.cc` 再读实现——断言就是该模块作者亲笔写下的行为契约，比任何文档都可靠。
