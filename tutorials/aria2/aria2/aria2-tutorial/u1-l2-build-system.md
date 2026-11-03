# 构建系统与依赖：从 configure.ac 到可执行文件

> 本讲是单元 1 的第 2 讲。上一讲（u1-l1）我们得到了一个结论：aria2 的功能与第三方库一一绑定，「configure 探测库 → 定义编译宏 → 条件编译 → `--version` 的 Features 行上报」，所以不同机器编译出的 aria2 特性集不同。本讲就来打开这个黑盒：**configure 到底探测了什么、探测结果如何变成编译宏和源码清单、源码又如何被组织成 `aria2c` 可执行文件**。读完本讲，你应当能在自己的机器上从源码编译出 aria2，并说清楚每一个构建开关的去向。

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `configure.ac`：理解 `ARIA2_ARG_WITH` / `ARIA2_ARG_WITHOUT` 宏的默认值语义、`PKG_CHECK_MODULES` 的探测方式、以及「显式要求但找不到就报错退出」的硬失败机制。
2. 说清探测结果的两条输出通道：`AC_DEFINE` 写入 `config.h`（供 C++ 源码 `#ifdef` 使用）与 `AM_CONDITIONAL` 驱动 `Makefile.am` 的条件块（决定哪些源文件参与编译）。
3. 理解 `Makefile.am` 如何组织 `src`、`test` 的编译单元：公共 `SRCS` 列表 + 一长串 `if` 条件块 + `libaria2.la` 中间产物 + `@VAR@` 替换的链接参数。
4. 掌握本机构建 aria2 的完整命令序列（`autoreconf -i && ./configure && make`），并能诊断常见编译失败原因。
5. 了解官方如何用 `build_test.sh` 验证 15 种特性组合、如何用 `Dockerfile.mingw` + `mingw-config` 交叉编译 Windows 二进制。

## 2. 前置知识

本讲涉及一些构建工具的概念，先用通俗语言过一遍：

- **编译与链接**：C++ 项目中，每个 `.cc` 文件先被编译成目标文件（`.o`），再由链接器把所有目标文件与第三方库（如 OpenSSL）拼成可执行文件。构建系统的工作就是自动化这两步。
- **静态库与动态库**：静态库（`.a`）在链接时被复制进可执行文件，产物大但独立；动态库（`.so` / `.dll`）在运行时加载，产物小但依赖目标机器上装有该库。aria2 官方 Windows 版用的是「全静态链接」（`ARIA2_STATIC=yes`），所以单个 `aria2c.exe` 拷到任何 Windows 上都能跑。
- **autotools 工具链**：一组 GNU 构建工具，分工如下：
  - **autoconf**：把 `configure.ac`（宏语言）展开成 `configure` 脚本（Shell 脚本）。`configure` 在用户机器上探测编译器、头文件、函数是否存在。
  - **automake**：把 `Makefile.am`（声明式的源码清单）生成 `Makefile.in` 模板；`configure` 再把探测结果填进模板，产出最终 `Makefile`。
  - **libtool**：抹平不同平台建库差异，产物是 `.la` 文件（可以理解为「跨平台库的抽象句柄」）。
  - **pkg-config**：查询第三方库编译/链接参数的标准工具。比如 `pkg-config --cflags --libs openssl` 会输出 `-I/usr/include/openssl -lssl -lcrypto`，构建系统不用硬编码路径。
- **交叉编译**：在 A 平台（如 Linux，称为 *build*）编译出运行在 B 平台（如 Windows，称为 *host*）的程序。需要 B 平台的编译器（如 `x86_64-w64-mingw32-g++`）和 B 平台版本的依赖库。
- **条件编译**：`#ifdef HAVE_ZLIB ... #endif`。宏存在才编译这段代码，这是 aria2 实现「库可选」的核心手段。

一句话总结 autotools 的数据流，后面 4.1 会反复用到：

```text
configure.ac ──autoconf──▶ configure 脚本 ──运行──▶ config.h + Makefile
Makefile.am  ──automake──▶ Makefile.in   ──运行──▶ config.h + Makefile
                                                      │
                                        make ──▶ aria2c 可执行文件
```

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `configure.ac` | autoconf 输入，1119 行，全部探测逻辑的总控 | 选项声明、依赖探测、编译宏与 Makefile 条件的生成 |
| `m4/aria2_arg.m4` | aria2 自定义的选项宏 | `ARIA2_ARG_WITH/WITHOUT` 的默认值与 `requested` 语义 |
| `Makefile.am` | 顶层构建声明（仅 39 行） | `SUBDIRS` 递归编译顺序 |
| `src/Makefile.am` | src 目录的编译单元组织（755 行） | `SRCS` 公共列表 + 条件块 + `libaria2.la` |
| `test/Makefile.am` | 测试程序组织 | 测试二进制如何复用 `libaria2.la` |
| `build_test.sh` | 官方多配置编译验证脚本 | 15 种「库组合 × 特性组合」 |
| `mingw-config` | Windows 交叉编译的 configure 包装脚本 | 官方 MinGW 构建的完整选项集 |
| `Dockerfile.mingw` | 用 Docker 复刻 Windows 官方构建环境 | 交叉编译依赖库的静态构建 |
| `README.rst` | 项目的「依赖-特性」对照表与构建说明 | `Dependency` 与 `How to build` 两节 |
| `src/FeatureConfig.cc` | 把编译宏上报为 `--version` 输出 | 闭环验证：宏 → 版本输出 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**构建流水线全景** → **configure.ac 特性探测** → **Makefile.am 源码组织** → **交叉编译与多配置验证**。

### 4.1 autotools 构建流水线全景

#### 4.1.1 概念说明

aria2 是一个支持 Linux/macOS/Windows（MinGW）/Android 等多平台的项目，而且大量依赖（TLS 库、XML 库、压缩库……）都是「可选」的。如果手写 `Makefile`，每个平台、每种依赖组合都要维护一份，很快就会失控。autotools 的价值在于：**把「探测环境」和「描述源码」分离**——`configure.ac` 只负责回答「这台机器上有什么」，`Makefile.am` 只负责回答「要编译哪些文件」，二者通过 `config.h`（宏）和 `@变量@`（参数）两个接口对接。

为什么 git 仓库里没有 `configure` 脚本？因为它是生成物。发行包（release tarball）里有预生成的 `configure`，而从 git 克隆的源码树只有 `configure.ac`，必须先跑 `autoreconf -i` 生成它——这是新手最常见的第一个卡点。

#### 4.1.2 核心流程

从 git 源码到可执行文件的完整流水线：

1. 开发者运行 `autoreconf -i`：autoconf 处理 `configure.ac` 生成 `configure`，automake 处理各目录 `Makefile.am` 生成 `Makefile.in`，libtool 生成 `ltmain.sh` 等辅助脚本。
2. 用户运行 `./configure`：脚本探测编译器、库、头文件、函数，产出：
   - `config.h`：全部 `HAVE_*` / `ENABLE_*` / `USE_*` 宏定义；
   - 每个 `Makefile.am` 对应目录下的 `Makefile`（探测结果填入 `Makefile.in` 的 `@变量@` 占位符）。
3. 用户运行 `make`：按 `Makefile` 编译所有源文件，链接成 `src/aria2c`（以及可选的 `libaria2.so`）。
4. `make check`（可选）：编译并运行 `test/aria2c` 测试二进制。

`configure.ac` 的骨架与文件头、以及它声明要生成哪些 `Makefile`，分别在这两处：

- [configure.ac:4-10](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L4-L10)：`AC_INIT([aria2],[1.37.0],...)` 登记项目名与版本号（版本号硬编码在这里，上一讲提过的 1.37.0 就来自此行）；`AM_INIT_AUTOMAKE` 启用 automake。
- [configure.ac:24-25](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L24-L25)：`AC_CONFIG_SRCDIR([src/a2io.h])` 用一个源文件标记源码树位置；`AC_CONFIG_HEADERS([config.h])` 声明「所有 `AC_DEFINE` 的宏都写进 config.h」——这是第一条输出通道的入口。
- [configure.ac:1052-1068](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L1052-L1068)：`AC_CONFIG_FILES` 列出要生成的全部 `Makefile`（顶层、`src/`、`test/`、`po/`、`lib/`、`deps/`、`doc/` 等），最后 `AC_OUTPUT` 落盘。

#### 4.1.3 源码精读

顶层 `Makefile.am` 只有一行核心声明：

- [Makefile.am:1](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/Makefile.am#L1)：`SUBDIRS =  po lib deps src doc test`——automake 的递归编译顺序。注意 `lib` 和 `deps` 排在 `src` 之前：`deps` 里是捆绑的 wslay（WebSocket 库）源码，`src` 链接时它必须已就绪。
- [Makefile.am:3](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/Makefile.am#L3)：`ACLOCAL_AMFLAGS = -I m4 --install`——告诉 aclocal 去项目自带的 `m4/` 目录找自定义宏（4.2 节的 `ARIA2_ARG_*` 就在那里），这是 git 构建必须先装 autoconf/automake/libtool 的原因之一。

而 README 的 `How to build` 一节给出的正是这条流水线的用户视角命令：

- [README.rst:226-241](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L226-L241)：说明从 git 仓库构建需要额外安装 autoconf、automake、autotools-dev、autopoint、libtool，然后运行 `autoreconf -i` 生成 configure 脚本。
- [README.rst:247-258](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L247-L258)：最简构建三步——`./configure`、`make`；静态链接则用 `./configure ARIA2_STATIC=yes`。

#### 4.1.4 代码实践

**实践：亲手生成 configure 脚本，观察产物变化。**

1. 实践目标：直观看到 `autoreconf -i` 生成了哪些文件，理解「git 源码 ≠ 可直接编译」。
2. 操作步骤：
   - 在项目根目录执行 `ls configure Makefile.in 2>&1`（git 树中应不存在或仅有残留）。
   - 执行 `autoreconf -i`（若提示缺工具，先安装 autoconf automake libtool autopoint gettext pkg-config）。
   - 再次 `ls configure Makefile.in src/Makefile.in m4/libtool.m4`，对比新生成的文件。
   - 执行 `./configure --help | head -40`，浏览生成的 configure 脚本支持的选项。
3. 需要观察的现象：`configure`、`Makefile.in`、`aclocal.m4`、`m4/` 下若干宏文件被创建；`configure --help` 输出里能看到 4.2 节将讲到的 `--without-gnutls`、`--disable-bittorrent` 等选项及其帮助文本。
4. 预期结果：`autoreconf -i` 无报错退出，`configure --help` 中出现 aria2 特有的选项分组。
5. 构建产物与运行结果：**待本地验证**（取决于机器上是否已装齐 autoconf 系工具）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 release tarball 里的源码可以直接 `./configure && make`，而 git 克隆的不行？

**答案**：tarball 在打包时已经运行过 `autoreconf`，预置了 `configure` 脚本和 `Makefile.in` 模板；git 仓库为了体积与生成物洁净，只保留 `configure.ac` 和 `Makefile.am` 这些「源文件」，所以必须先 `autoreconf -i` 现场生成（见 [README.rst:226-241](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L226-L241)）。

**练习 2**：顶层 `SUBDIRS` 的顺序是 `po lib deps src doc test`，为什么 `deps` 要排在 `src` 前面？

**答案**：automake 按 `SUBDIRS` 顺序递归进入子目录构建（[Makefile.am:1](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/Makefile.am#L1)）。`deps` 下的 wslay 会先编译出 `libwslay.la`，`src` 链接 `libaria2.la` 时才能引用它（WebSocket 特性启用时，见 4.2.3 的 wslay 小节）。

### 4.2 configure.ac 特性探测：ARIA2_ARG 宏与依赖矩阵

#### 4.2.1 概念说明

`configure.ac` 的前 100 行声明了所有构建开关，中间约 500 行逐个探测依赖，最后把结果汇总打印。理解它的钥匙是 `m4/aria2_arg.m4` 里四个自定义宏的**默认值语义**：

- `ARIA2_ARG_WITH(pkg)`：默认**不用**（`with_pkg=no`），要启用需显式 `--with-pkg`。典型如 `--with-libuv`。
- `ARIA2_ARG_WITHOUT(pkg)`：默认**尝试用**（`with_pkg=yes`，即「check」），不想要就 `--without-pkg`。绝大多数依赖用这个，对应 README 说的「configure 会尽可能多启用功能」。
- 二者都会在用户**显式**给出选项时额外设置 `with_pkg_requested` 变量——这是「软失败 vs 硬失败」的分水岭：
  - 默认尝试（未显式要求）的库找不到 → 只打警告，功能静默关闭；
  - 显式 `--with-pkg` 要求的库找不到 → `ARIA2_DEP_NOT_MET` 直接报错退出 configure。

这套设计让默认构建「宽容」（缺库就裁剪自己），又让定制构建「严格」（指名要的库缺了就失败，避免静默产出残缺二进制）。

#### 4.2.2 核心流程

一次 `./configure` 内部对某个依赖库的处理套路：

```text
读取 with_pkg（默认 yes 或 --without-pkg 改为 no）
   │
   ├─ no ──▶ 跳过，功能关闭
   └─ yes ─▶ PKG_CHECK_MODULES(pkg-config 查询)
                ├─ 成功 ──▶ AC_DEFINE([HAVE_PKG])   → 写入 config.h（源码 #ifdef）
                │           AC_SUBST([PKG_CFLAGS/LIBS]) → 传给 Makefile（-I / -l）
                │           AM_CONDITIONAL([HAVE_PKG]) → Makefile.am 的 if 块开关
                └─ 失败 ──▶ with_pkg_requested=yes?
                               ├─ 是 ──▶ ARIA2_DEP_NOT_MET → 报错退出
                               └─ 否 ──▶ AC_MSG_WARN 警告后继续
```

探测完成后还有一层**特性推导**：BitTorrent/Metalink/XML-RPC/异步 DNS 等大特性不是直接开关，而是「依赖满足才可启用」：

```text
Metalink   = (libxml2 或 expat 存在) 且 未 --disable-metalink
XML-RPC    = libxml2 或 expat 存在
异步 DNS   = c-ares 存在且版本 ≥ 1.16.0
消息摘要后端 use_md = apple ▸ nettle ▸ gcrypt ▸ openssl ▸ internal（按优先级择一）
```

#### 4.2.3 源码精读

**（1）选项声明区** —— [configure.ac:53-77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L53-L77) 集中声明所有依赖开关：`ARIA2_ARG_WITH([libuv])` 是「默认关」，而 gnutls、openssl、libxml2、libssh2 等一整排 `ARIA2_ARG_WITHOUT` 是「默认探测」；[configure.ac:71-77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L71-L77) 的 `--disable-ssl/bittorrent/metalink/websocket/epoll` 与 `--enable-libaria2` 则是功能级开关（libaria2 默认不构建）。

**（2）两个自定义宏** —— [m4/aria2_arg.m4:4-16](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/m4/aria2_arg.m4#L4-L16) 是 `ARIA2_ARG_WITH`/`ARIA2_ARG_WITHOUT` 的定义，注意第 7 行与第 15 行的默认值差异（`no` vs `yes`），以及两处都同时设置 `with_$1_requested`；[m4/aria2_arg.m4:37-46](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/m4/aria2_arg.m4#L37-L46) 定义硬失败宏 `ARIA2_DEP_NOT_MET`（依赖缺失）与 `ARIA2_FET_NOT_SUPPORTED`（特性因依赖不满足而无法启用）。

**（3）探测模板实例：zlib** —— [configure.ac:211-237](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L211-L237) 展示了完整的两段式探测：先 `PKG_CHECK_MODULES([ZLIB], [zlib >= 1.2.3])` 走 pkg-config；失败再降级用 `AC_CHECK_LIB([z], [zlibVersion])` 直接试链函数（应对老系统没有 `.pc` 文件的情况）；成功后 `AC_DEFINE([HAVE_ZLIB])` 写宏，并追加检查 `gzbuffer` 等新函数是否存在。

**（4）互斥与优先级** —— XML 解析器二选一：[configure.ac:261-262](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L261-L262) 中 expat 的探测条件是 `have_libxml2 != yes`，即 libxml2 优先（README 注记「libxml2 has precedence over Expat」的源码依据）。TLS 后端同理按 apple → win → gnutls → openssl 的顺序择一：[configure.ac:384-385](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L384-L385) gnutls 的探测门是 `have_ssl != yes`，[configure.ac:405-406](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L405-L406) openssl 又在 gnutls 之后——所以想让 aria2 用 OpenSSL 必须显式 `--without-gnutls`（见 [README.rst:142-144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L142-L144)）。另外 [configure.ac:316-323](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L316-L323) 显示 `--disable-ssl` 会一次性把所有 TLS 相关 `with_*` 变量清零，这是「总开关级联」的实现方式。

**（5）消息摘要后端选择** —— [configure.ac:520-542](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L520-L542) 的嵌套 if 推导出唯一的 `use_md` 值并定义 `USE_APPLE_MD`/`USE_LIBNETTLE_MD`/`USE_LIBGCRYPT_MD`/`USE_OPENSSL_MD`/`USE_INTERNAL_MD` 五选一宏——全都没有时退回内部实现（只支持 md5/sha1，对应 [README.rst:162-163](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L162-L163) 的说明）。这是 4.3 节 Makefile 条件块和单元 5 校验讲义的共同伏笔。

**（6）特性推导与捆绑库** —— [configure.ac:587-610](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L587-L610)：`ENABLE_BITTORRENT` 与 `ENABLE_METALINK` 宏在此定义，显式要求但依赖不满足时走 `ARIA2_FET_NOT_SUPPORTED` 硬失败；[configure.ac:995-1006](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L995-L1006)：启用 WebSocket 时用 `AC_CONFIG_SUBDIRS([deps/wslay])` 把源码树里捆绑的 wslay 作为子项目纳入构建，`WSLAY_CFLAGS/WSLAY_LIBS` 指向其构建产物。

**（7）探测汇总表** —— [configure.ac:1070-1119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L1070-L1119)：configure 结束前用 `AC_MSG_NOTICE` 打印「summary of build options」，逐行列出每个库/特性的 yes/no、SSL 选择、消息摘要后端、是否静态构建等。**这张表就是本讲综合实践要记录的产物**。

**（8）闭环验证** —— config.h 里的宏最终被谁消费？[FeatureConfig.cc:40-72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L40-L72) 用 `#ifdef HAVE_ZLIB`、`#ifdef HAVE_LIBSSH2` 等决定 include 哪些第三方头文件；[FeatureConfig.cc:191-248](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L191-L248) 的 `usedLibs()` 函数把每个存在的库拼成 `zlib/1.3.1 GnuTLS/3.8.0 ...` 字符串——这正是上一讲看到的 `aria2c --version` Features 行的来源。至此「探测 → 宏 → 条件编译 → 版本上报」链条完整闭环。

#### 4.2.4 代码实践

**实践：用 configure 开关做特性裁剪，记录汇总表。**

1. 实践目标：亲手改变特性集，验证「不同配置产出不同 aria2」。
2. 操作步骤：
   - 运行 `./configure 2>&1 | tail -60`，把最后的「summary of build options」存为 `summary-default.txt`。
   - 运行 `./configure --without-gnutls --without-libxml2 2>&1 | tail -60`，存为 `summary-trimmed.txt`。
   - `diff summary-default.txt summary-trimmed.txt`，观察 GnuTLS、LibXML2、Metalink、XML-RPC 等行从 yes 变 no。
   - 再试一次 `./configure --with-openssl --without-gnutls`，确认 OpenSSL 行变 yes。
3. 需要观察的现象：裁剪后 Metalink/XML-RPC 联动变为 no（因为两个 XML 库都关了）；显式 `--with-一个不存在的库`（如 `--with-libuv` 且机器没装）会触发 `ARIA2_DEP_NOT_MET` 的报错退出。
4. 预期结果：两份 summary 的 diff 恰好对应被裁剪的依赖及其派生特性。
5. 汇总表的具体行值：**待本地验证**（取决于本机安装了哪些开发包）。

#### 4.2.5 小练习与答案

**练习 1**：`--without-libxml2` 之后 Metalink 一定会被关闭吗？

**答案**：不一定。Metalink 需要 libxml2 **或** expat 任一（[configure.ac:599-610](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L599-L610) 的判断是 `have_libxml2 || have_libexpat`）。关掉 libxml2 后若系统装有 expat，expat 的探测条件（[configure.ac:261-262](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L261-L262)）即成立，Metalink 仍可用——这也正是 build_test.sh 里 `--without-libxml2` 组合后缀叫 `expat` 的原因。

**练习 2**：为什么用户没有显式传 `--with-libxml2` 时，configure 找不到 libxml2 只警告不报错？

**答案**：因为 `ARIA2_ARG_WITHOUT` 声明的库默认值是 `yes`（尝试探测），只有显式给出选项才会设置 `with_libxml2_requested`（[m4/aria2_arg.m4:12-16](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/m4/aria2_arg.m4#L12-L16)）。configure.ac 中所有 `ARIA2_DEP_NOT_MET` 调用都被 `if requested = yes` 包着——默认探测失败走软路径（警告 + 功能关闭），显式要求失败走硬路径（退出）。

**练习 3**：`--disable-ssl`、`--without-gnutls`、`--without-openssl` 三个选项的作用范围有何区别？

**答案**：`--disable-ssl` 是总开关，直接把 appletls/wintls/gnutls/openssl/nettle/gcrypt 的 `with_*` 全部清零（[configure.ac:316-323](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L316-L323)），最终产出无 HTTPS 的二进制；`--without-gnutls` 只把 GnuTLS 从 TLS 后端候选中排除，通常导致探测落到 OpenSSL；`--without-openssl` 同理只是排除 OpenSSL 一个候选。前者消灭整个特性，后两者只是调整后端选择。

### 4.3 Makefile.am 源码组织：条件编译与可执行文件装配

#### 4.3.1 概念说明

如果说 configure.ac 回答「有什么」，`src/Makefile.am` 就回答「编什么」。它用一份**公共 `SRCS` 列表**（所有配置都必须编译的核心源码，约 270 个文件）加上**几十个 `if` 条件块**（按 configure 探测结果追加可选源码）来拼出最终的编译清单。这带来一个重要洞察：**阅读 src 目录时要意识到，约 900 个源文件并不是每次构建全部参与编译**——`Bt*`、`DHT*`、`Metalink*` 这些前缀文件族都住在条件块里，只有对应特性启用才会编译。这也解释了上一讲的源码地图为何按前缀分族。

另一个关键设计：无论是否构建 libaria2，所有源码都先编成一个 libtool 库 `libaria2.la`——命令行程序 `aria2c` 只是 `main.cc` 加上对这个库的链接。这让「命令行工具」和「C 嵌入库」共享同一份实现（单元 8 的 libaria2 讲义会用到这一点）。

#### 4.3.2 核心流程

`src/Makefile.am` 的装配逻辑：

```text
bin_PROGRAMS = aria2c                 # 产出可执行文件
aria2c_SOURCES = main.cc              # aria2c 自己只有 main.cc

SRCS = <约 270 个公共源文件>           # 核心引擎 + HTTP + FTP 命令链…
if ENABLE_WEBSOCKET   → SRCS += WebSocket*
if !ENABLE_WEBSOCKET  → SRCS += NullWebSocketSessionMan.h   # 空实现兜底
if HAVE_LIBXML2       → SRCS += Xml2XmlParser
if HAVE_LIBEXPAT      → SRCS += ExpatXmlParser
if HAVE_EPOLL         → SRCS += EpollEventPoll
if ENABLE_BITTORRENT  → SRCS += <Bt*、DHT*、Peer* 约 180 个文件>
if ENABLE_METALINK    → SRCS += Metalink*
if HAVE_LIBSSH2       → SRCS += Sftp*、SSHSession
if !HAVE_ASCTIME_R 等 → SRCS += 对应兼容实现        # 平台函数缺失时补齐
if ENABLE_LIBARIA2    → lib_LTLIBRARIES  = libaria2.la  # 安装为共享库
else                  → noinst_LTLIBRARIES = libaria2.la # 仅内部使用

libaria2_la_SOURCES = $(SRCS)
aria2c 链接 ← libaria2.la ← @OPENSSL_LIBS@ @ZLIB_LIBS@ …（AC_SUBST 注入）
```

test 目录完全复用这个库：`test/aria2c`（测试二进制，与主程序同名但目录不同）链接 `../src/libaria2.la` 再加 CppUnit。

#### 4.3.3 源码精读

**（1）程序骨架与公共列表** —— [src/Makefile.am:1-3](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L1-L3)：`bin_PROGRAMS = aria2c` 且 `aria2c_SOURCES = main.cc`——可执行文件的「自有代码」只有入口文件一个。[src/Makefile.am:4-10](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L4-L10) 起是 `SRCS` 公共列表的开头，按字母序列出了所有配置都必须编译的引擎/工具/HTTP/FTP 源码，一直延续到 [src/Makefile.am:274](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L274)。

**（2）条件块三例** —— WebSocket 的「启用 + 空实现兜底」模式：[src/Makefile.am:280-290](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L280-L290)，禁用时用 `NullWebSocketSessionMan.h` 顶替，保证其余代码引用的接口永远存在（这是可选特性的通用手法）；XML 解析器后端：[src/Makefile.am:292-305](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L292-L305)，`HAVE_SOME_XMLLIB` 提供公共 `XmlParser` 骨架，`HAVE_LIBXML2`/`HAVE_LIBEXPAT` 各自追加具体后端；事件轮询后端：[src/Makefile.am:320-322](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L320-L322)（epoll）与 [src/Makefile.am:664-678](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L664-L678)（poll/port/kqueue/libuv）——单元 2 的 EventPoll 讲义会看到这些文件内部如何实现同一接口。

**（3）大特性块** —— BitTorrent 是最大的一块：[src/Makefile.am:430-608](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L430-L608) 一次性追加约 180 个 `Bt*`、`DHT*`、`Peer*`、`Lpd*`、`UTPex*` 等文件（单元 6、7 的全部素材）；Metalink 在 [src/Makefile.am:610-626](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L610-L626)；SFTP 依赖 libssh2，在 [src/Makefile.am:416-422](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L416-L422)；zlib 派生的 gzip 解码在 [src/Makefile.am:402-408](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L402-L408)。

**（4）平台兼容垫片** —— [src/Makefile.am:628-662](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L628-L662)：configure.ac 探测到的 `!HAVE_ASCTIME_R`、`!HAVE_GETADDRINFO`、`!HAVE_STRPTIME` 等平台函数缺失，会在这里追加 aria2 自带的同名替代实现——这就是 configure.ac 后半部分大量 `AC_CHECK_FUNCS` 的用途。

**（5）libaria2 的双形态** —— [src/Makefile.am:682-696](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L682-L696)：`--enable-libaria2` 时 `libaria2.la` 是被安装的 `lib_LTLIBRARIES`（并额外编入 `aria2api.cc` 等 API 文件），否则是仅供内部链接的 `noinst_LTLIBRARIES`；无论哪种形态，[src/Makefile.am:696](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L696) 都令 `libaria2_la_SOURCES = $(SRCS)`。

**（6）编译/链接参数注入** —— [src/Makefile.am:699-718](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L699-L718) 的 `AM_CPPFLAGS` 与 [src/Makefile.am:724-742](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L724-L742) 的 `EXTLDADD` 把 configure 探测出的 `@ZLIB_CFLAGS@`、`@OPENSSL_LIBS@`、`@WSLAY_LIBS@` 等变量原样填进编译/链接命令——`AC_SUBST` 的变量与这里的 `@...@` 占位符一一对应，这是第二条输出通道（参数通道）的终点。最后 [src/Makefile.am:744](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L744) `LDADD = libaria2.la` 让 `aria2c` 链上这个库。

**（7）测试的复用** —— [test/Makefile.am:1-4](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L1-L4)：`TESTS = aria2c` 声明 `make check` 要跑的可执行文件，其源码从 `AllTest.cc`（CppUnit 测试注册总入口）开始；测试源码同样按特性裁剪，如 [test/Makefile.am:120-220](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L120-L220) 的 `ENABLE_BITTORRENT` 块收录全部 BT/DHT 测试；[test/Makefile.am:244-264](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L244-L264) 的 `aria2c_LDADD` 链接 `../src/libaria2.la` 加上全部第三方库与 `@CPPUNIT_LIBS@`——测试与主程序共享同一个引擎库。

#### 4.3.4 代码实践

**实践：观察条件块在生成后的 Makefile 中如何「坍缩」。**

1. 实践目标：把 4.2 的探测结果与 4.3 的编译清单在真实 Makefile 里对上号。
2. 操作步骤：
   - 完成 `./configure` 后打开生成的 `src/Makefile`，搜索 `libaria2_la_SOURCES` 赋值，确认它是一份已按本机特性展开的文件清单。
   - 在 `src/Makefile` 中搜索 `am__libaria2_la_SOURCES_DIST`（automake 保存的「全量可能清单」），对比它与实际 `libaria2_la_SOURCES` 的差异——被裁剪特性（如本机未启用的某后端）的文件会出现在前者而不在后者。
   - 执行 `make -n src/aria2c 2>/dev/null | tail -3` 或 `make -C src -n aria2c | tail -3`，查看链接命令里是否出现 `libaria2.la` 及本机启用的 `-l` 库。
3. 需要观察的现象：不同 `./configure` 参数后重新生成 `src/Makefile`，`libaria2_la_SOURCES` 的内容会随之增减（例如 `--disable-bittorrent` 后所有 `Bt*.cc` 消失）。
4. 预期结果：能看到链接命令以 `main.o` + `libaria2.la` + 一串 `-l<lib>` 的形式组装出 `aria2c`。
5. 具体命令输出：**待本地验证**（不同 automake 版本生成的变量命名略有差异）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `aria2c_SOURCES` 只有 `main.cc` 一个文件，最终的可执行文件却包含全部引擎代码？

**答案**：[src/Makefile.am:744](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L744) 用 `LDADD = libaria2.la` 让 aria2c 链接 libtool 库，而该库的源码由 `libaria2_la_SOURCES = $(SRCS)`（[src/Makefile.am:696](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L696)）提供——即所有公共与条件源码都先进库，可执行文件只是「入口 + 库」。

**练习 2**：一个没装 zlib 的机器编译出的 aria2，HTTP 下载 gzip 响应会怎样？从 Makefile.am 找依据。

**答案**：无法解码。gzip 解码相关源码 `GZipDecodingStreamFilter` 等在 `HAVE_ZLIB` 条件块内（[src/Makefile.am:402-408](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L402-L408)），没有 zlib 时这些代码根本不参与编译，特性在二进制层面就不存在，而非运行时降级。

**练习 3**：`test/Makefile.am` 里的测试二进制也叫 `aria2c`，与 `src/aria2c` 会冲突吗？

**答案**：不会。automake 的程序放在各自目录构建：`src/Makefile.am` 产出 `src/aria2c`，`test/Makefile.am` 产出 `test/aria2c`（`check_PROGRAMS`，仅在 `make check` 时构建运行，见 [test/Makefile.am:1-4](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L1-L4)）。两者路径不同、用途不同，后者是 CppUnit 测试 runner。

### 4.4 交叉编译与多配置验证：build_test.sh 与 Dockerfile.mingw

#### 4.4.1 概念问题

两个工程问题催生了本模块的两类脚本：

1. **特性组合爆炸**：每个依赖都可开可关，全排列成百上千种，任何一种组合都可能编译失败。aria2 的对策是 `build_test.sh`——挑出 15 种有代表性的组合逐一 `configure + make check`，在合入代码前验证「裁剪路径」没有腐烂。
2. **交叉编译环境的搭建成本**：在 Linux 上产出 Windows 二进制，需要 MinGW 编译器和一整套 Windows 版静态依赖库。aria2 把整个过程固化进 `Dockerfile.mingw` 与 `mingw-config` 包装脚本，任何有 Docker 的机器都能一条命令复刻官方构建。

#### 4.4.2 核心流程

`build_test.sh` 的单元流程：

```text
build "<configure 选项>" "<产物后缀>"
  ├─ ./configure $选项          # 日志存 /tmp/aria2buildtest/configure_后缀.log
  ├─ make clean
  ├─ make -j2 check             # 编译并跑测试
  └─ cp src/aria2c → aria2c_后缀  # 成品二进制按后缀归档
```

Dockerfile.mingw 的分层流程：

```text
ubuntu:22.04 基础镜像
  ├─ apt 安装 mingw-w64 交叉工具链 + autotools
  ├─ 下载并静态编译 6 个依赖（gmp/expat/sqlite3/zlib/c-ares/libssh2）
  │    → 全部安装到 /usr/local/$HOST 前缀
  └─ git clone aria2 → autoreconf -i → ./mingw-config → make → strip
```

#### 4.4.3 源码精读

**（1）build_test.sh 的构建函数** —— [build_test.sh:16-35](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/build_test.sh#L16-L35)：`build()` 依次执行 configure（tee 保存日志、拷贝 `config.log`）、`make clean`、`make -j2 check`，成功后把 `src/aria2c` 归档为 `aria2c_$2`；已存在的产物会被跳过以支持断点续跑。

**（2）15 种组合矩阵** —— [build_test.sh:49-67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/build_test.sh#L49-L67)：前半是「库组合」——`nodigest`（去掉全部摘要库）、`libgcrypt`、`openssl`、`nossl`、`nocares`、`expat`、`noxml`、`nozlib`、`nosqlite3`、`nolibssh2`；后半是「特性组合」——`nobt`、`noml`、`nobt_noml`、`noepoll`、`noepoll_nocares`、`libaria2`。每一行都是 4.2 节某个探测分支的端到端验证。这份清单也是学习 configure 开关的现成教材。

**（3）MinGW 官方构建的 configure 选项** —— [mingw-config:61-83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/mingw-config#L61-L83)：`./configure --host=$HOST` 声明交叉目标；`ARIA2_STATIC=yes` 全静态；`--without-gnutls --without-openssl`（Windows 上不依赖第三方 TLS）；`--without-libxml2 --with-libexpat`（用 expat）；`PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"` 把 pkg-config 指向交叉安装的依赖——把 4.2 学到的每个开关在真实场景里用了一遍。

**（4）Dockerfile.mingw 的环境装配** —— [Dockerfile.mingw:12-17](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/Dockerfile.mingw#L12-L17)：基础镜像与 `ARG HOST=i686-w64-mingw32`（改 `--build-arg HOST=x86_64-w64-mingw32` 即可产 64 位）；[Dockerfile.mingw:23-37](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/Dockerfile.mingw#L23-L37)：安装 `gcc-mingw-w64` 交叉工具链并下载 6 个依赖源码包；[Dockerfile.mingw:39-106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/Dockerfile.mingw#L39-L106)：逐个用 `--host=$HOST --disable-shared --enable-static` 静态编译安装到 `/usr/local/$HOST`——每个依赖自身的构建同样用 autotools，与 aria2 主项目同一套路；[Dockerfile.mingw:108-114](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/Dockerfile.mingw#L108-L114)：最后克隆 aria2 源码，`autoreconf -i && ./mingw-config && make -j$(nproc)`，再 `$HOST-strip src/aria2c.exe` 去符号减体积。

#### 4.4.4 代码实践

**实践：从 build_test.sh 挑一个组合在本机复现（不跑 Docker）。**

1. 实践目标：体会「特性组合」的真实含义，而不只看清单。
2. 操作步骤：
   - 阅读 [build_test.sh:49-67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/build_test.sh#L49-L67)，挑一个本机依赖能满足的组合，例如 `--disable-bittorrent --disable-metalink`（对应后缀 `nobt_noml`）。
   - 执行 `./configure --disable-bittorrent --disable-metalink 2>&1 | tail -60`，确认汇总表中 `Bittorrent: no`、`Metalink: no`。
   - 执行 `make -j$(nproc)`（可比 build_test.sh 更快），完成后 `./src/aria2c --version | head -5`，对照 Features 行确认 BitTorrent/Metalink 已消失。
3. 需要观察的现象：编译文件数量明显减少（BT 一族约 180 个文件不再参与）；`--version` 输出的 Features 与 configure 汇总一一对应——4.2.3 第（8）点的闭环在你自己的二进制上得到验证。
4. 预期结果：构建成功且版本输出的特性列表与配置一致。
5. 若想完整复刻官方 Windows 构建：`sudo docker build -t aria2-mingw - < Dockerfile.mingw`（见 [Dockerfile.mingw:4-10](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/Dockerfile.mingw#L4-L10) 的注释），耗时较长，**待本地验证**、可选。

#### 4.4.5 小练习与答案

**练习 1**：`build_test.sh` 为什么要 `make check` 而不仅是 `make`？

**答案**：编译通过只说明代码能构建，`make check` 还会运行 CppUnit 测试（[build_test.sh:29](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/build_test.sh#L29) 用 `make -j2 check` 并把 `test/aria2c.log` 追加进归档日志）。特性组合的裁剪很容易破坏「禁用 A 时启用 B」这类交叉路径，只有跑测试才能发现。

**练习 2**：Dockerfile.mingw 里为什么每个依赖库都要 `--disable-shared --enable-static`（如 [Dockerfile.mingw:39-49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/Dockerfile.mingw#L39-L49) 的 gmp）？

**答案**：因为最终目标是 `ARIA2_STATIC=yes` 的全静态 `aria2c.exe`（见 [mingw-config:78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/mingw-config#L78)）。静态链接要求依赖以静态库（`.a`）形式存在；若只编出 Windows 动态库（`.dll`），目标机器没装这些 DLL 就无法运行，就失去了「单文件绿色版」的意义。

## 5. 综合实践

**任务：在你的机器上完整构建 aria2，产出一份「特性-配置」对照报告。**

1. 安装基础工具链与开发包（参考 [README.rst:195-235](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L195-L235)：Debian/Ubuntu 系装 `build-essential pkg-config autoconf automake autotools-dev autopoint libtool`，再按需装 `libgnutls-dev nettle-dev libgmp-dev libssh2-1-dev libc-ares-dev libxml2-dev zlib1g-dev libsqlite3-dev libcppunit-dev`）。
2. 执行 `autoreconf -i && ./configure && make -j$(nproc)`，保存 configure 末尾的「summary of build options」。
3. 把汇总表整理成三列笔记：**特性 / 是否启用 / 依据的 configure.ac 行号或选项**（例如：HTTPS / yes / GnuTLS 探测于 configure.ac:384-403；Metalink / yes / 推导于 configure.ac:599-610）。
4. 故意制造一次失败：`./configure --with-libuv`（若本机未装 libuv），观察 `ARIA2_DEP_NOT_MET` 报错（[m4/aria2_arg.m4:37-38](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/m4/aria2_arg.m4#L37-L38)）；再故意用老编译器或对比 `AX_CXX_COMPILE_STDCXX([11], [], [mandatory])`（[configure.ac:144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L144)）理解 C++11 是硬性要求。
5. 运行 `./src/aria2c --version`，把 Features 行与第 3 步的表格逐项对照——它们必须完全一致，因为二者同源于 config.h 宏（[FeatureConfig.cc:191-248](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L191-L248)）。
6. （可选进阶）重新 `./configure --without-gnutls --without-libxml2 --disable-bittorrent` 再构建一次，对比两次 `--version` 的 Features 差异。

**验收标准**：两份 Features 行的差异能逐项追溯到 configure 选项与 configure.ac 的探测分支，你的构建报告就完成了。若某些步骤因环境无法执行，如实标注「待本地验证」。

## 6. 本讲小结

- aria2 用 autotools 实现「一份源码、任意裁剪」：`configure.ac` 探测环境（`ARIA2_ARG_WITH` 默认不用、`ARIA2_ARG_WITHOUT` 默认探测，显式要求失败即硬退出），`Makefile.am` 按探测结果拼装编译清单。
- 探测结果通过**三条通道**生效：`AC_DEFINE` 写入 `config.h` 供源码 `#ifdef`；`AM_CONDITIONAL` 打开 `Makefile.am` 的 `if` 块决定哪些源文件参与编译；`AC_SUBST` 以 `@VAR@` 注入编译/链接参数。
- `src/Makefile.am` 的核心是「公共 `SRCS` + 数十个条件块」：BitTorrent 约 180 个文件、Metalink、SFTP、各 TLS 后端、各 EventPoll 后端全部按需编入；所有源码先进 `libaria2.la`，`aria2c` 只是 `main.cc` + 这个库。
- 依赖之间存在优先级与互斥：libxml2 优先于 expat，TLS 后端按 apple → win → gnutls → openssl 择一，消息摘要后端五选一兜底到 internal 实现。
- 官方用 `build_test.sh` 验证 15 种特性组合，用 `Dockerfile.mingw` + `mingw-config` 把 Windows 交叉编译（静态依赖链 + `ARIA2_STATIC=yes`）固化为一键流程。
- git 源码构建的入口命令是 `autoreconf -i && ./configure && make`；`configure` 结束打印的 summary 与 `aria2c --version` 的 Features 行同源，可互相印证。

## 7. 下一步学习建议

构建跑通后，下一讲（u1-l3「进程入口：main.cc 与启动流程」）将从你刚编译出的 `src/aria2c` 入手，跟踪 `main()` → `Platform` → `Context` → `MultiUrlRequestInfo` 的启动调用链。建议在此之前：

1. 用 `ls src/*.cc | wc -l` 与生成的 `src/Makefile` 中实际编译的文件数对比，直观感受条件编译裁掉了多少源码。
2. 通读 [README.rst:108-182](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L108-L182) 的 Dependency 一节，把特性-依赖表与 4.2 的探测分支互相印证。
3. 若你打算后续做二次开发（单元 9），现在就可以试试 `make check`，确认测试体系在你的构建上可用。
