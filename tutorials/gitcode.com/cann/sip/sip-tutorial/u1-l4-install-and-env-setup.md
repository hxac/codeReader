# 安装产物、run 包与环境变量

## 1. 本讲目标

上一讲（u1-l3）我们已经用 `bash build.sh` 把 SiP 编译了出来。本讲回答编译之后的问题：**编译产出了什么、怎么交给最终用户、用户装完之后靠什么找到库**。学完本讲，你应该能够：

1. 说出 `output/` 目录里每类产物（头文件、动态库、静态库、脚本）的来源与用途。
2. 解释 makeself 打包出的 `.run` 自解压包的结构，以及它与「直接用 output 目录」两种交付方式的差异。
3. 使用 `install.sh` 的 `--install / --install-path= / --uninstall / --upgrade` 完成安装、卸载与升级。
4. 说清 `set_env.sh` 背后的 `ASDSIP_HOME_PATH` 与 `LD_LIBRARY_PATH` 两个环境变量的作用，并亲手编译一个链接 `libasdsip.so` 的最小程序验证环境生效。

## 2. 前置知识

阅读本讲前，你需要了解以下概念（不熟悉也没关系，下面用通俗语言解释）：

- **动态库与静态库**：Linux 下 `.so` 是动态库，程序运行时才被加载，系统靠 `LD_LIBRARY_PATH` 环境变量（加上系统默认路径）查找它；`.a` 是静态库，编译时直接把代码拷进可执行文件。SiP 两者都提供。
- **自解压包（run 包）**：把一堆文件打成一个可执行文件，运行时自己解压、再执行包内的安装脚本。CANN 生态大量使用这种形式（例如 CANN toolkit 本身的 `.run` 安装包），SiP 沿用了这一交付习惯，打包工具是开源的 **makeself**。
- **软链接（symlink）与 `latest` 目录**：安装后会出现 `<安装根>/latest -> <版本号>` 的软链接。程序和环境变量只认 `latest`，升级新版本时只需把软链指向新版本目录，用户侧无需任何改动。
- **`source` 与环境变量**：`source xxx.sh` 是在**当前 shell** 里逐行执行脚本，因此脚本里的 `export` 会留在当前终端会话中；文档里也说这是「进程级」生效——关掉终端就失效。
- **`ASCEND_HOME_PATH`**：CANN toolkit 安装后由它的 `set_env.sh` 导出，指向 CANN 根目录。SiP 的公开头文件引用了 `acl/acl.h` 等属于 CANN 的头文件，所以**编译和安装 SiP 之前必须先 source CANN 的环境**（u1-l3 已建立这一认知）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `build.sh` | 编译总调度脚本；其 `fn_make_run_package()` 负责生成 run 包（本讲重点之一） |
| `scripts/install.sh` | run 包内的安装器模板；被打包脚本做占位符替换后成为真正的安装入口 |
| `scripts/uninstall.sh` | run 包内的卸载器模板，随包安装到 `<版本目录>/scripts/` 下 |
| `scripts/filelist.csv` | 安装文件清单，卸载时按它精确删除文件 |
| `scripts/help.info` | run 包的自述帮助信息（makeself 的 `--help-header`） |
| `scripts/set_env.sh` | 安装后提供的环境变量脚本，只有 8 行，是「环境变量」模块的主角 |
| `CMakeLists.txt` / `core/CMakeLists.txt` / `ops/CMakeLists.txt` | 定义编译产物如何被 `make install` 收进 `output/` |
| `docs/header_files_library_files.md` | 官方头文件/库文件说明，是理解产物清单的权威文档 |
| `docs/zh/Installation_Operation_Guide/environment_variable.md` | 官方环境变量参考 |
| `version.info`（仓库根） | 记录当前版本号与依赖组件的版本要求 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**run 包结构** → **安装脚本** → **环境变量**。三者正好构成一条交付链：编译产出被组织成 run 包，run 包由安装脚本落到磁盘，最终由环境变量把磁盘上的库「接」给应用程序。

### 4.1 run 包结构：从 output 目录到 .run 文件

#### 4.1.1 概念说明

`bash build.sh` 跑完后，仓库下会多出一个 `output/` 目录。它不是随便堆放的中间产物，而是一份**可直接交付的安装载荷（payload）**：头文件放 `include/`、库文件放 `lib/`，外加安装脚本与版本信息。run 包则是把这份载荷用 makeself 压缩封装成一个自解压可执行文件，方便分发到目标机器。

为什么要分「直接用 output」和「run 包安装」两种方式？

- **开发者**：改代码、反复编译，直接引用 `output/` 最省事，不污染系统目录。
- **最终用户**：拿到的是一台陌生机器上的一个 `.run` 文件，需要标准的安装/卸载/升级流程、固定的安装路径、收紧的文件权限——这就是 run 包存在的意义。

#### 4.1.2 核心流程

`build.sh` 的 `--dev` 主线（默认类型）按以下顺序工作：

```text
fn_build()
  ├── 准备三方依赖 mki / catlass（u1-l3 已讲）
  └── fn_compile_and_pack()
        ├── cmake .. && make -j64 && make install
        │     └── make install 把库和头文件装进 CMAKE_INSTALL_PREFIX（默认 output/）
        └── fn_make_run_package()
              ├── 识别 CPU 架构（x86_64 / aarch64，不允许其他）
              ├── 生成 version.info（版本、平台、分支、commit id）
              ├── 把 install.sh / set_env.sh 拷进 output/
              ├── 对脚本里的占位符做 sed 替换（架构、版本、日志路径）
              └── 调 makeself.sh 把 output/ 打成 .run，启动脚本指定为 ./install.sh
```

一句话总结：**CMake 负责产出载荷，`fn_make_run_package()` 负责给载荷套上安装外壳。**

#### 4.1.3 源码精读

**（1）安装前缀：为什么产物落在 `output/`**

根 CMakeLists 把默认安装前缀固定为源码根下的 `output`：

[ CMakeLists.txt:L27-L29](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/CMakeLists.txt#L27-L29)

这段代码的意思：若用户没有显式指定 `CMAKE_INSTALL_PREFIX`，就强制设为 `${PROJECT_SOURCE_DIR}/output`。因此 `make install` 的所有产物都汇聚到仓库的 `output/` 目录。

**（2）载荷里有哪些库：三条 install 规则**

- 主用户库 `libasdsip.so` / `libasdsip_static.a` 与全部公开头文件由 core 模块安装：

[core/CMakeLists.txt:L48-L53](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/core/CMakeLists.txt#L48-L53)

这段代码安装 `asdsip`（动态/静态主库）到 `lib/`，并把 `blas_api.h`、`fft_api.h`、`base_api.h`、`filter_api.h`、`interp_api.h`、`asdsip.h` 和 `domain/` 目录安装到 `include/`——这正好就是 [docs/header_files_library_files.md:L22-L30](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/header_files_library_files.md#L22-L30) 表格里列出的那批头文件。

- 算子核心运行时库 `libasdsip_core.so` 与主机端工具库 `libasdsip_host.so` 由 ops 模块安装：

[ops/CMakeLists.txt:L67-L74](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/CMakeLists.txt#L67-L74)

- MKI 框架库由根 CMakeLists 直接拷贝：

[CMakeLists.txt:L88-L89](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/CMakeLists.txt#L88-L89)

四个库的分工，官方文档 [docs/header_files_library_files.md:L38-L44](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/header_files_library_files.md#L38-L44) 给出了权威说明，整理成下表：

| 库文件 | 角色 | 用户是否直接链接 |
| --- | --- | --- |
| `libasdsip.so` / `libasdsip_static.a` | 主用户库，聚合全部公开 API | 是（唯一入口） |
| `libasdsip_core.so` | 算子注册、Kernel 加载调度、tiling 等 Host 核心，被主库自动依赖 | 否 |
| `libasdsip_host.so` | 主机端参数处理等辅助功能 | 否 |
| `libmki.so` / `libmki_static.a` | MKI 内核抽象框架（u3-l4 会讲），随包附带 | 否 |

此外还有一类**不属于本仓库**的运行时依赖（`libascendcl.so`、`libaclnn.so`），来自 CANN 安装目录，见 [docs/header_files_library_files.md:L45-L53](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/header_files_library_files.md#L45-L53)。这解释了为什么应用程序最终需要同时「看得见」SiP 的 lib 和 CANN 的 lib64。

**（3）`fn_make_run_package()`：run 包诞生的全过程**

[build.sh:L46-L57](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/build.sh#L46-L57)

这段代码用 `uname -a` 判断当前是 x86_64 还是 aarch64，两者都不是就直接退出——run 包是**按 CPU 架构绑定**的，x86 打的包装不到 arm 机器上（后面 install.sh 还会再校验一次）。

[build.sh:L58-L67](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/build.sh#L58-L67)

这段代码把版本号、平台、git 分支与 commit id 写进 `output/version.info`。注意它与**仓库根**的 [version.info:L1-L7](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/version.info#L1-L7) 不是一回事：仓库根的那份记录版本号与依赖组件版本要求（构建输入），output 里这份记录本次构建的确切来源（构建指纹）。排查「这台机器上装的到底是哪个 commit 编的」时就靠它。构建脚本中的版本常量定义在：

[build.sh:L368-L371](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/build.sh#L368-L371)

即当前版本 `9.1.0`，安装日志路径 `/var/log/cann_asdsip_log/`，日志文件名 `cann_asdsip_install.log`。

[build.sh:L73-L93](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/build.sh#L73-L93)

这段代码做两件事：把 `install.sh`、`set_env.sh` 拷进载荷；然后把 `uninstall.sh`、`filelist.csv` 连同两个脚本再拷到 `output/scripts/`，并用 `sed` 把脚本里的占位符替换成真实值——`ASDSIPPKGARCH` 换成 `uname -m` 的架构、`VERSION_PLACEHOLDER` 换成版本号、`LOG_PATH_PLACEHOLDER`/`LOG_NAME_PLACEHOLDER` 换成日志路径与文件名。也就是说，仓库里的 `scripts/install.sh` 是**模板**，真正进包的是替换后的副本。

[build.sh:L95-L98](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/build.sh#L95-L98)

这就是 makeself 打包调用：makeself 工具本身**来自 CANN toolkit 安装目录**（`$ASCEND_HOME_PATH/toolkit/tools/...`），把 `$CODE_ROOT/output` 压缩成 `Ascend-cann-SIP_<版本>_linux-<架构>.run`，并指定解压后自动执行 `./install.sh`、使用 `scripts/help.info` 作为帮助页。这也再次印证 u1-l3 的结论：没有 CANN 环境，连打包这一步都过不去。

**（4）交付方式对比**

| 维度 | 一键编译（直接用 output/） | run 包安装 |
| --- | --- | --- |
| 产物形态 | 仓库内 `output/` 目录 | 单个 `.run` 文件，可拷贝分发 |
| 库的引用方式 | 手动 `-I output/include -L output/lib` | `source <安装根>/set_env.sh` |
| 版本管理 | 无 | `<安装根>/<版本>/` + `latest` 软链 |
| 卸载/升级 | 手动 `rm -rf output` | `--uninstall` / `--upgrade` |
| 文件权限 | 开发者权限 | 脚本 550、库与头文件 440 等收紧策略 |

#### 4.1.4 代码实践

**实践目标**：亲眼确认 run 包里装的就是 output 载荷，并看懂 version.info 的内容。

**操作步骤**：

1. 确认已完成 u1-l3 的完整编译（产物在 `output/`）。
2. 查看 output 目录结构（示例命令，待本地验证）：

   ```bash
   ls output/            # 预期看到 include lib scripts install.sh set_env.sh version.info
   cat output/version.info
   ```

3. 查看 run 包的帮助与自述（不安装，只看信息）：

   ```bash
   output/Ascend-cann-SIP_9.1.0_linux-x86_64.run --help   # 文件名中的架构按实际机器调整
   ```

4. 对比 `scripts/filelist.csv` 列出的文件与 `output/` 中实际存在的文件：

   [scripts/filelist.csv:L1-L20](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/scripts/filelist.csv#L1-L20)

**需要观察的现象**：`version.info` 中的 `commit id` 与 `git rev-parse HEAD` 输出一致；`--help` 输出的内容就是 [scripts/help.info:L1-L5](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/scripts/help.info#L1-L5) 的五行选项说明。

**预期结果**：filelist.csv 中的每个相对路径都能在 `output/` 下找到对应文件。若在无 NPU/CANN 的机器上操作，编译步骤无法完成，本实践只能「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `libasdsip_core.so` 明明在安装目录的 `lib/` 里，官方文档却说用户「通常无需单独引用」？

**答案**：`libasdsip.so` 在链接期声明了对 `libasdsip_core.so` 的依赖（core/CMakeLists.txt 中 `target_link_libraries(asdsip ... asdsip_core)`），用户程序只链接 `libasdsip.so`，运行时动态加载器会顺着依赖关系自动加载 `libasdsip_core.so`——前提是 `LD_LIBRARY_PATH` 能找到它，这正是后面 `set_env.sh` 做的事。

**练习 2**：在 x86_64 机器上编译出的 run 包直接拷到 aarch64 机器安装，会发生什么？

**答案**：安装会失败。`install.sh` 的 `install_process()` 会比对包内记录的架构（`ASDSIPPKGARCH`，由 build.sh 用 `uname -m` 在打包时替换）与当前机器架构，不一致时报 "pkg arch ... is not consistent with the current enviroment architecture" 并退出（见 4.2.3 的源码）。此外 kernel 二进制本身也是按架构编译的（u1-l3 讲过架构开关）。

**练习 3**：`.run` 文件执行后为什么会自动开始安装，而不是只解压？

**答案**：makeself 打包时把启动脚本指定为 `./install.sh`（build.sh 第 98 行最后一个参数），`.run` 自解压完成后默认执行该脚本；想只解压不安装，可用 makeself 的 `--target <dir>` 选项（待本地验证）。

### 4.2 安装脚本：install.sh 与 uninstall.sh

#### 4.2.1 概念说明

`install.sh` 是 run 包解压后的第一个执行者，是一个带完整生命周期管理的安装器：支持安装、卸载、升级、指定路径、全用户安装五种模式。它的两个设计值得学习：

1. **模板 + 占位符**：源码里写的是 `VERSION_PLACEHOLDER`、`ASDSIPPKGARCH` 这类占位符，构建时由 sed 注入真实值（见 4.1.3），一份脚本服务所有版本与架构。
2. **按清单卸载**：安装了什么，记录在 `filelist.csv`；卸载时逐行删除，删完再清理空目录，最大限度不留垃圾。

#### 4.2.2 核心流程

`install.sh` 的主流程（`main()` 分发）：

```text
main()
  ├── parse_script_args()      解析 --install / --install-path= / --uninstall / --upgrade / --install-for-all
  ├── 分支一 --uninstall：check_uninstall_path → uninstall()
  │     └── 从 latest/version.info 读出旧版本号 → uninstall_process(<根>/<旧版本>)
  ├── 分支二 --upgrade：备份旧版本 → 卸载 → 安装新版 → 删除备份（失败自动回滚）
  └── 分支三 安装：log_init → check_owner → install_process → chmod_authority
        ├── check_owner：必须有 ASCEND_HOME_PATH；非 root 默认装 ~/Ascend/asdsip
        ├── install_process：校验架构、路径必须是绝对路径
        ├── install_to_path：装到 <安装根>/<版本>/，把 set_env.sh 提到 <安装根>/，
        │                   并建立 latest → <版本> 软链
        └── chmod_authority：按文件类型收紧权限
```

#### 4.2.3 源码精读

**（1）默认路径与占位符**

[scripts/install.sh:L21-L28](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/scripts/install.sh#L21-L28)

这段代码定义默认安装根 `/usr/local/Ascend/asdsip`（root 用户）以及三个等待构建期替换的占位符变量。日志路径按用户身份分流：

[scripts/install.sh:L31-L37](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/scripts/install.sh#L31-L37)

root 用户日志写 `/var/log/cann_asdsip_log/`，普通用户写 `$HOME/var/log/cann_asdsip_log/`，日志超过 50MB 停止写入（`MAX_LOG_SIZE` 在第 29 行定义）。注意这是**安装日志**，与库运行时的日志（4.3 节）是两回事。

**（2）参数解析：`--install-path` 会自动追加 `asdsip`**

[scripts/install.sh:L188-L226](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/scripts/install.sh#L188-L226)

注意 `--install-path=*` 分支：传入 `/home/me/sip-test` 后 `target_dir` 变成 `/home/me/sip-test/asdsip`——**脚本会在你给的路径后面自动补一层 `asdsip`**，这是实践时最容易踩的坑。

**（3）前置检查：没有 CANN 环境不让装**

[scripts/install.sh:L442-L472](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/scripts/install.sh#L442-L472)

`check_owner()` 做三件事：要求 `ASCEND_HOME_PATH` 已设置且目录存在（否则直接报错退出）；要求当前用户与 CANN 安装目录属主一致；确定最终安装根——非 root 且未指定路径时改为 `${HOME}/Ascend/asdsip`，指定了 `--install-path` 则完全尊重用户路径。

**（4）架构与绝对路径校验，然后落盘**

[scripts/install.sh:L418-L440](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/scripts/install.sh#L418-L440)

这段代码比对包架构与机器架构，并强制 `--install-path` 使用绝对路径。

[scripts/install.sh:L398-L416](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/scripts/install.sh#L398-L416)

`install_to_path()` 是安装的核心四步：

1. 安装目录定为 `<安装根>/<版本号>`，若已存在先卸载（保证幂等）；
2. `copy_files()` 把解压目录整体拷入（`cp -r ${sourcedir}/*`，`sourcedir` 是 run 包自解压出的临时目录）；
3. 把 `set_env.sh` 从版本目录**上移到安装根**——因为版本目录会随升级变化，而环境脚本必须稳定；
4. `ln -snf $VERSION latest` 建立指向当前版本的软链。

安装后的目录结构（以自定义路径 `$HOME/sip-test` 为例）：

```text
$HOME/sip-test/asdsip/            ← 安装根
├── set_env.sh                    ← 环境变量脚本（稳定入口）
├── latest -> 9.1.0               ← 软链，永远指向最新版本
└── 9.1.0/                        ← 版本目录（载荷本体）
    ├── include/                  ← asdsip.h 及六大模块头文件
    ├── lib/                      ← libasdsip.so 等四个库
    ├── scripts/
    │   ├── uninstall.sh          ← 卸载脚本（占位符已替换）
    │   └── filelist.csv          ← 文件清单
    ├── install.sh
    └── version.info
```

**（5）权限收紧**

[scripts/install.sh:L140-L186](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/scripts/install.sh#L140-L186)

`chmod_authority()` 按类型赋权：`.sh` 脚本 550、`.so`/`.a`/`.h` 等 440、目录 550；`--install-for-all` 模式下给其他用户补上读执行位（`chmod_recursion` 内的注释说明了这一意图）。这是交付类软件的常见安全实践：用户能读能执行，但不能改。

**（6）卸载：按清单删除**

[scripts/install.sh:L474-L485](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/scripts/install.sh#L474-L485)

`uninstall()` 先从 `latest/version.info` 解析出已装版本号，再对 `<安装根>/<版本>` 执行 `uninstall_process()`。

[scripts/install.sh:L301-L354](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/scripts/install.sh#L301-L354)

`delete_installed_files()` 逐行读 `filelist.csv` 删除文件，随后再删一份「历史版本遗漏文件」硬编码清单——这是对老版本升级路径的兜底。若 `filelist.csv` 不存在，退化为整目录删除。

随包安装的独立卸载器 `scripts/uninstall.sh` 逻辑相同，只是入口不同：

[scripts/uninstall.sh:L153-L154](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/scripts/uninstall.sh#L153-L154)

它从自身位置反推安装目录（`scripts/` 的上两级拼接版本号），因此可以在不记得安装参数的情况下直接执行 `<安装根>/<版本>/scripts/uninstall.sh` 卸载；同时 `delete_latest()` 负责清掉 `latest` 软链与安装根下的 `set_env.sh`（[scripts/uninstall.sh:L106-L116](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/scripts/uninstall.sh#L106-L116)）。

#### 4.2.4 代码实践

**实践目标**：把 run 包安装到自定义目录，观察「版本目录 + latest 软链 + set_env.sh 上移」三件事真实发生。

**操作步骤**：

1. 先 source CANN 环境（`check_owner` 的硬性要求）：

   ```bash
   source /usr/local/Ascend/ascend-toolkit/set_env.sh   # 路径按实际 CANN 安装位置调整
   ```

2. 安装到自定义目录（注意实际会落在 `$HOME/sip-test/asdsip` 下）：

   ```bash
   chmod +x output/Ascend-cann-SIP_9.1.0_linux-x86_64.run
   ./output/Ascend-cann-SIP_9.1.0_linux-x86_64.run --install --install-path=$HOME/sip-test
   ```

3. 检查安装结果：

   ```bash
   ls -l $HOME/sip-test/asdsip/          # 应看到 set_env.sh、latest、9.1.0
   readlink $HOME/sip-test/asdsip/latest # 应输出 9.1.0
   cat $HOME/sip-test/asdsip/9.1.0/version.info
   ```

4. 卸载验证（二选一）：

   ```bash
   ./output/Ascend-cann-SIP_9.1.0_linux-x86_64.run --uninstall
   # 或
   bash $HOME/sip-test/asdsip/9.1.0/scripts/uninstall.sh
   ```

**需要观察的现象**：安装日志输出 "Ascend-cann-asdsip install success!"；非 root 用户可在 `$HOME/var/log/cann_asdsip_log/cann_asdsip_install.log` 看到完整安装记录；卸载后 `$HOME/sip-test/asdsip` 被清空删除。

**预期结果**：如上；本实践依赖真实构建产物与 CANN 环境，在纯阅读环境标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `set_env.sh` 要从版本目录移动到安装根，而 `uninstall.sh` 留在版本目录的 `scripts/` 里？

**答案**：`set_env.sh` 是用户长期 source 的稳定入口，若放在版本目录里，升级后路径就变了，用户的 `.bashrc` 会失效；放安装根 + `latest` 软链则永远有效。`uninstall.sh` 只在卸载某个具体版本时用，跟着版本目录走恰好正确。

**练习 2**：升级（`--upgrade`）过程中途断电，旧版本还能找回来吗？

**答案**：能。`upgrade()` 先执行 `back_up_old_version()` 把旧版本目录复制为 `<版本>_recover`、备份 `set_env.sh`；安装脚本通过 `trap exit_solver EXIT` 捕获失败，`recover_flag` 为 y 时调用 `recover_old_version()` 回滚。只有升级成功后 `remove_back_up_version()` 才删除备份。

**练习 3**：普通用户（非 root）不加 `--install-path` 直接 `--install`，会装到哪里？

**答案**：`${HOME}/Ascend/asdsip`。`check_owner()` 中非 root 且 `--install` 时把 `default_install_path` 改写为用户主目录下的路径，避免普通用户向 `/usr/local/Ascend` 写入失败。

### 4.3 环境变量：set_env.sh 的 8 行与运行日志开关

#### 4.3.1 概念说明

安装完成后，磁盘上有了头文件和库，但编译器默认不知道去哪找它们。SiP 的解决方案极其克制：一个只有 8 行的 `set_env.sh`，导出两个环境变量。官方文档 [docs/zh/Installation_Operation_Guide/environment_variable.md:L19-L27](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/zh/Installation_Operation_Guide/environment_variable.md#L19-L27) 把这两个变量列为「基础环境变量」：

| 环境变量 | 作用 |
| --- | --- |
| `ASDSIP_HOME_PATH` | 软件包安装后文件存储路径，编译时用作 `-I`/`-L` 的前缀 |
| `LD_LIBRARY_PATH` | Linux 加载动态库的搜索路径，运行时靠它找到 `libasdsip.so` |

此外还有一组**运行时日志**环境变量（`ASCEND_PROCESS_LOG_PATH` 等，见 [docs/zh/Installation_Operation_Guide/environment_variable.md:L39-L55](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/zh/Installation_Operation_Guide/environment_variable.md#L39-L55)），控制库运行时日志的落盘路径与级别——它们的实现机制（`ASDSIP_LOG` 宏与 env 读取）属于 u2-l5 的主题，本讲只需知道入口。

#### 4.3.2 核心流程

`set_env.sh` 的执行逻辑：

```text
source set_env.sh
  ├── 用 BASH_SOURCE 定位脚本自身所在目录（无论从哪里 source 都正确）
  ├── ASDSIP_HOME_PATH = <脚本目录>/latest/     ← 注意带尾部斜杠，且指向软链
  └── LD_LIBRARY_PATH = $ASDSIP_HOME_PATH/lib : <原值>   ← 前置追加，不动已有路径
```

两个设计点：用 `${BASH_SOURCE[0]}` 而非 `$PWD` 定位自己，保证 `source` 时不受当前目录影响；追加而非覆盖 `LD_LIBRARY_PATH`，不破坏 CANN 等其他环境。

#### 4.3.3 源码精读

完整源码只有 8 行：

[scripts/set_env.sh:L1-L8](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/scripts/set_env.sh#L1-L8)

逐行解读：

- 第 1 行取 `${BASH_SOURCE[0]}`——被 `source` 执行时 `$0` 是 shell 本身，只有 `BASH_SOURCE` 才指向脚本文件；
- 第 2 行校验文件名确实是 `set_env.sh`，防止被错误方式引用；
- 第 3-4 行求出脚本所在目录并导出 `ASDSIP_HOME_PATH` 指向 `latest/`（**软链**而非具体版本号，这就是升级对用户透明的原因；注意值带尾部 `/`，拼路径时写 `$ASDSIP_HOME_PATH/lib` 会得到双斜杠，Linux 下无害）；
- 第 5 行把 `$ASDSIP_HOME_PATH/lib` 前置进 `LD_LIBRARY_PATH`。

有了这两个变量，编译与运行就都有了着落：

```bash
# 编译：头文件在 $ASDSIP_HOME_PATH/include，库在 $ASDSIP_HOME_PATH/lib
g++ app.cpp -I$ASDSIP_HOME_PATH/include -L$ASDSIP_HOME_PATH/lib -lasdsip
# 运行：加载器沿 LD_LIBRARY_PATH 找到 libasdsip.so 及其依赖 libasdsip_core.so
./a.out
```

还要记住本讲在 4.1.3 埋下的一个事实：`asdsip.h` 聚合的 `blas_api.h` 依赖 CANN 的 `acl/acl.h` 与 `aclnn` 头文件，因此编译时的 include 路径除了 SiP 的，还要带上 CANN 的（根 CMakeLists 的 include 配置印证了这一点：[CMakeLists.txt:L53-L62](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/CMakeLists.txt#L53-L62)）。

#### 4.3.4 代码实践

**实践目标**：验证「source 之后，一个空程序能编译链接上 `libasdsip.so`」——这是环境变量生效的最直接证据。

**操作步骤**：

1. 编写最小程序 `hello_asdsip.cpp`（**示例代码**，非仓库原有文件）：

   ```cpp
   #include "asdsip.h"

   int main() {
       return 0;   // 本实践只验证「能编过、能链上」，不调用任何算子
   }
   ```

   注意：`asdsip.h` 及其聚合头文件是 C++ 头文件，所以用 `g++` 编译（任务原文写 gcc，但 gcc 不默认链接 `libstdc++`，需要额外 `-lstdc++`，推荐直接用 `g++`）。

2. 依次 source 两份环境脚本：

   ```bash
   source /usr/local/Ascend/ascend-toolkit/set_env.sh   # CANN：提供 ASCEND_HOME_PATH 与 libascendcl
   source $HOME/sip-test/asdsip/set_env.sh              # SiP：提供 ASDSIP_HOME_PATH 与 LD_LIBRARY_PATH
   ```

3. 编译并链接：

   ```bash
   g++ hello_asdsip.cpp \
       -I$ASDSIP_HOME_PATH/include \
       -I$ASCEND_HOME_PATH/include -I$ASCEND_HOME_PATH/include/aclnn \
       -L$ASDSIP_HOME_PATH/lib -lasdsip \
       -o hello_asdsip
   ```

4. 验证动态库解析情况：

   ```bash
   echo $ASDSIP_HOME_PATH        # 预期：.../asdsip/latest/
   echo $LD_LIBRARY_PATH         # 预期开头：.../asdsip/latest/lib:...
   ldd hello_asdsip | grep asdsip
   ```

**需要观察的现象**：`ldd` 输出中 `libasdsip.so` 与 `libasdsip_core.so` 的路径解析到 `<安装根>/latest/lib/` 下，而不是 "not found"。

**预期结果**：编译零错误，程序可执行（本实践不调用算子，无 NPU 机器上 `./hello_asdsip` 通常也能正常退出；若调用真实算子则必须有 NPU 硬件）。完整流程依赖已构建的 run 包，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：把 `set_env.sh` 拷贝改名为 `myenv.sh` 再 source，会发生什么？

**答案**：打印 "There is no 'set_env.sh' to import" 并且**不导出任何变量**。脚本第 2 行用正则校验文件名必须匹配 `set_env.sh`，这是一道防止误用的保险丝。

**练习 2**：用户 source 了 `set_env.sh` 之后管理员升级了 SiP（`latest` 改指向新版本目录），用户需要重新 source 吗？

**答案**：不需要。`ASDSIP_HOME_PATH` 指向的是 `latest/` 这个软链而非具体版本目录，每次编译/运行时解析软链得到的都是当前最新版本，这正是 4.2 中「set_env.sh 上移 + latest 软链」设计的收益。（已打开的进程仍持有旧库的内存映射，属 Linux 通用行为。）

**练习 3**：为什么 `LD_LIBRARY_PATH` 要**前置**追加而不是写成覆盖？

**答案**：前置保证 SiP 的库目录优先级最高（同名库时先用本包的），同时保留原有条目——用户机器上通常还 source 了 CANN 的环境，`libascendcl.so` 等就靠原有路径解析；覆盖会直接破坏 CANN 乃至其他软件的库查找。

## 5. 综合实践

把本讲三个模块串成一个完整的「交付闭环」任务（在具备 CANN 环境的机器上完成，纯阅读环境可作为步骤清单保留待执行）：

1. **构建**：`bash build.sh`，确认 `output/` 下生成 `include/`、`lib/`、`version.info` 与 `Ascend-cann-SIP_9.1.0_linux-<架构>.run`（对应最小模块「run 包结构」）。
2. **安装**：`source` CANN 环境后，执行 run 包 `--install --install-path=$HOME/sip-lab`，检查 `$HOME/sip-lab/asdsip/` 下出现 `9.1.0/`、`latest` 软链与上移的 `set_env.sh`，并核对 `version.info` 的 commit id（对应「安装脚本」）。
3. **接环境**：`source $HOME/sip-lab/asdsip/set_env.sh`，打印 `ASDSIP_HOME_PATH` 与 `LD_LIBRARY_PATH` 确认值（对应「环境变量」）。
4. **验证**：编译 4.3.4 的 `hello_asdsip.cpp` 并用 `ldd` 确认库解析路径。
5. **收尾**：执行 `<安装根>/9.1.0/scripts/uninstall.sh` 卸载，确认安装根被清空；对比卸载前后 `filelist.csv` 所列文件与磁盘实况，理解按清单卸载的行为。

完成后的自查清单：能否不看讲义说出 run 包里有什么、`--install-path` 为何会多出 `asdsip` 一层、`ASDSIP_HOME_PATH` 为何指向 `latest` 而不是版本号？三问都能答，本讲就过关了。

## 6. 本讲小结

- `build.sh` 的 `fn_make_run_package()` 把 CMake 装进 `output/` 的载荷（头文件 + 四个库 + 脚本 + version.info）用 makeself 打成按架构区分的 `.run` 自解压包，启动脚本为 `install.sh`。
- 一键编译面向开发者（直接引用 `output/`），run 包面向最终用户（安装/卸载/升级/权限管理），两者载荷相同、外壳不同。
- `install.sh` 是「模板 + sed 占位符替换」的安装器：默认 root 装 `/usr/local/Ascend/asdsip`、非 root 装 `~/Ascend/asdsip`，`--install-path` 会自动追加 `asdsip`；安装产生 `<版本目录> + latest 软链 + 上移的 set_env.sh` 三件套。
- 卸载按 `filelist.csv` 清单逐文件删除并兜底清理历史遗留文件；升级先备份、失败自动回滚。
- `set_env.sh` 只有 8 行，导出 `ASDSIP_HOME_PATH`（指向 `latest/`，编译期路径前缀）与前置追加 `LD_LIBRARY_PATH`（运行期库查找），是应用程序使用 asdsip 的唯一环境入口。
- 编译包含 `asdsip.h` 的程序时，除 SiP 的 include 外还必须带上 CANN 的 `$ASCEND_HOME_PATH/include`（及其 `aclnn` 子目录），因为公开头文件依赖 `acl/acl.h`。

## 7. 下一步学习建议

下一讲 **u1-l5（第一个算子调用：跑通 example）** 将第一次真正「用起来」本讲装好的库：走读 `example/example.cpp`，掌握 ACL 初始化、aclTensor 创建、handle/plan/workspace/stream 的固定调用顺序。建议提前浏览 `example/` 目录与 `example/build.sh`，观察它的编译命令是如何同时引用 SiP 与 CANN 两套 include/lib 路径的——那正是本讲 4.3 内容的实战化。后续若想深入了解运行时日志环境变量（`ASCEND_GLOBAL_LOG_LEVEL` 等）的实现原理，可预习 u2-l5。
