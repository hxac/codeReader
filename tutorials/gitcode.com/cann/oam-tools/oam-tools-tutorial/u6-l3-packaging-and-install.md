# u6-l3 打包与安装升级：.run 包生命周期

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出一个 `cann-oam-tools_<版本>_linux-<架构>.run` 安装包从 CMake 配置到落盘 `build_out/` 的完整生成链路，以及 `version.cmake` 在其中的位置。
2. 读懂 `scripts/package/` 目录下的打包描述（`oam_tools.xml`）与安装脚本族（`install.sh`、`opp_install.sh`、`opp_upgrade.sh`、`opp_uninstall.sh`、`uninstall.sh`、`msprof_install.sh`）各自承担的职责。
3. 理解 `install.sh` 如何用同一个入口分发 `--full/--run/--devel`（安装）、`--upgrade`（升级）、`--uninstall`（卸载）三种生命周期操作。
4. 理解 `test/st/install`、`test/st/upgrade`、`test/st/uninstall` 三组 ST 如何以黑盒方式看护 .run 包的安装产物、升级兼容与卸载残留。

## 2. 前置知识

- **.run 包**：Linux 上一种"自解压 + 自安装"的可执行 shell 归档（本仓用 makeself 技术生成）。`chmod +x` 后直接 `./xxx.run --full` 即可安装，等价于"压缩包里内置了一个安装器"。CANN 全家桶（toolkit、nnae、opp 包）都用这种形态发布。
- **makeself staging 目录**：CPack 在真正打 .run 前，先把所有待发布文件收集到一个暂存目录（本仓为 `build/_CPack_Packages/makeself_staging`），再把暂存目录连同安装脚本一起塞进 makeself 壳。
- **CPack 组件（COMPONENT）**：CMake 的 `install(... COMPONENT oam-tools)` 规则决定"哪些文件进包、落到哪个相对路径"。CPack 按组件收集后再交给打包后端（这里是 makeself）。
- **cann-cmake 公共函数库**：u1-l2 讲过，本仓配置期从 OBS 拉取的 CMake 函数集。本讲会用到它的 `set_cann_package`、`set_cann_cpack_config` 等函数——这些函数不在本仓库里，但调用点在。
- **ascend_install.info / version.info / scene.info**：安装后落在 `share/info/oam_tools/` 下的三个元数据文件。`version.info` 记录包版本，`scene.info` 记录目标架构（安装器用它做架构一致性检查），`ascend_install.info` 记录安装类型、安装用户、安装路径等"已安装状态"，升级/卸载都靠读它。
- **ST（System Test）**：u6-l1 讲过 UT/ST 之分。本讲的 ST 是纯黑盒——把 .run 包当外部命令执行，只断言退出码、输出关键字和磁盘产物，不 import 任何项目源码。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
| --- | --- |
| `version.cmake` | 声明包名、版本号（9.1.0）与 runtime/metadef 依赖约束，是版本信息的唯一源头 |
| `cmake/package.cmake` | `pack_built_in()` 函数：把 `scripts/package/oam_tools/scripts/` 下的安装脚本、cann-cmake 公共安装库、version.info 装进 CPack 组件，最后调 `set_cann_cpack_config` 生成 .run |
| `scripts/package/oam_tools/oam_tools.xml` | 打包描述文件：声明安装入口脚本、各产物（tools/hccl_test、tools/profiler、asys、aml so 等）的目标路径与权限、目录树 |
| `scripts/package/oam_tools/scripts/install.sh` | .run 包内安装器总入口，分发 install/upgrade/uninstall 三种操作 |
| `scripts/package/oam_tools/scripts/opp_install.sh` | 安装的具体执行者：调 cann-cmake 的 `install_common_parser.sh --copy_all` 拷贝文件并收紧权限 |
| `scripts/package/oam_tools/scripts/opp_upgrade.sh` / `opp_uninstall.sh` | 升级 / 卸载的具体执行者 |
| `scripts/package/oam_tools/scripts/uninstall.sh` | 安装后释放到 `cann/cann_uninstall.sh` 的"用户友好卸载入口"，内部反向转调 install.sh |
| `scripts/package/oam_tools/scripts/msprof_install.sh` | 安装/升级后解包 msprof wheel 到 `tools/profiler/profiler_tool/` |
| `scripts/package/oam_tools/scripts/oam_common.sh` | 安装脚本族的公共函数：日志、相对软链接创建、批量 chmod |
| `scripts/package/module/ascend/*.xml` | 打包期引用的 cann-cmake 公共 block 配置（EngineeringCommon、ToolsCommon、DetectInfo 等） |
| `test/st/conftest.py` | `run_package` / `install_dir` 两个共享 fixture：找包、建干净安装根目录 |
| `test/st/install/testcase/test_install_st.py` | 安装 ST：三种安装类型 + `--noexec --extract` + 解压/安装一致性看护 |
| `test/st/upgrade/testcase/test_upgrade_st.py` | 升级 ST：4 种"旧类型→新类型"组合升级 |
| `test/st/uninstall/testcase/test_uninstall_st.py` | 卸载 ST：两种卸载方法 + 残留文件检查 |
| `test/st/uninstall/testcase/conftest.py` | `installed_dir` fixture：先装好再测卸载 |
| `build.sh` | 打包驱动：`make package` 后把 `cann*.run` 搬到 `build_out/` |
| `scripts/run_tests.sh` | ST 用例组入口：`install_st` / `upgrade_st` / `uninstall_st` 三个 case 名 |

## 4. 核心概念与源码讲解

### 4.1 scripts/package：打包描述与安装脚本族

#### 4.1.1 概念说明

`scripts/package/` 不是"被编译的源码"，而是随 .run 包一起发布的**安装期资产**，分两部分：

- `oam_tools/oam_tools.xml`：给 cann-cmake 打包器看的**声明式描述**——包叫什么、入口脚本在哪、每个文件落到目标机的哪个路径、什么权限。
- `oam_tools/scripts/*.sh`：真正在用户机器上跑的**安装脚本族**。

这套"XML 描述 + 脚本执行"的分离，让"装什么"（数据）和"怎么装"（逻辑）解耦：新增一个发布物通常只改 XML；改安装流程则动脚本。

#### 4.1.2 核心流程

一个 .run 包被用户执行后的脚本调用链：

```
./cann-oam-tools_xxx.run --full
  └─ install.sh                    # 总入口：解析参数、校验环境、分发
       ├─ install → opp_install.sh # 调 install_common_parser.sh --copy_all 拷文件
       │              └─ msprof_install.sh   # 装完后解包 msprof wheel
       ├─ upgrade → opp_upgrade.sh
       └─ uninstall → opp_uninstall.sh

cann/cann_uninstall.sh              # 安装后释放的用户入口（即 uninstall.sh）
  └─ 反向转调 install.sh --uninstall
```

#### 4.1.3 源码精读

先看 XML 描述的头部，它声明了安装入口和元数据：

[scripts/package/oam_tools/oam_tools.xml:L2-L15](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/oam_tools.xml#L2-L15)

这段是包的"身份证"：`suffix=run` 说明产物是 .run 包；`install_script` 指向上文调用链里的 `install.sh`；`cleanup` 指定打包期清理脚本。`generate_info` 两段则在安装时**动态生成** `oam_tools_version.h`（版本头文件）和 `scene.info`（含 arch，供安装器做架构检查）。

再看发布物到目标路径的映射——四大组件都在这里落位：

[scripts/package/oam_tools/oam_tools.xml:L30-L40](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/oam_tools.xml#L30-L40)

这三段 `file_info` 把 hccl_test/profiler/operator_cmp、msaicerr、asys 分别释放到 `tools/`、`tools/ascend_system_advisor/` 下——这正对应 u1-l3 讲过的"安装后工具目录布局"。`optional="true"` 的条目（如 operator_cmp）缺失不阻断打包。

脚本族的公共函数集中在 `oam_common.sh`，最核心的是日志函数：

[scripts/package/oam_tools/scripts/oam_common.sh:L35-L41](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830b6ae6577137c1eb78a4d/scripts/package/oam_tools/scripts/oam_common.sh#L35-L41)

`logandprint` 实现了"双通道"输出：终端通道受 `--quiet` 静音（但 ERROR/WARN/INFO 级别可穿透），日志文件通道永远全量写入 `ascend_install.log`。所有安装脚本都 source 这个文件，保证日志行为一致。注意 ST 测试断言的 `[ERROR]` 关键字正是从这里打出来的。

#### 4.1.4 代码实践

**实践目标**：确认 XML 描述与实际发布物的一一对应关系。

**操作步骤**：

1. 打开 `scripts/package/oam_tools/oam_tools.xml`，列出所有 `file_info` 段的 `dst_path`。
2. 对照 `cmake/package.cmake` 与根 `CMakeLists.txt` 中的 `install(... COMPONENT oam-tools)` 规则，检查哪些路径同时出现在两侧。
3. 思考：`dir_info` 段（[oam_tools.xml:L90-L111](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/oam_tools.xml#L90-L111)）里那些目录为什么要显式声明权限（如 `share/info/oam_tools` 为 750）？

**需要观察的现象**：`tools/profiler/profiler_tool`、`tools/ascend_system_advisor/asys` 等目录在 XML 的 `dir_info` 与 ST 测试断言路径中同时出现——描述、安装、验证三方共享同一套目录契约。

**预期结果**：得出一张"发布物 → XML 条目 → 目标路径"对照表。本实践为纯源码阅读，无需设备。

#### 4.1.5 小练习与答案

**练习 1**：`oam_tools.xml` 中 `install_script` 指向的脚本是如何进入 .run 包的？

**答案**：不是 XML 自己复制的。`cmake/package.cmake` 的 `install(DIRECTORY ${script_prefix}/ DESTINATION share/info/oam_tools/script ...)`（[cmake/package.cmake:L74-L86](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/package.cmake#L74-L86)）把整个 scripts 目录装进 CPack 组件，XML 只是告诉打包器"包内哪个脚本是入口"。

**练习 2**：为什么 `logandprint` 在 `--quiet` 模式下仍允许 ERROR/WARN/INFO 上屏？

**答案**：`--quiet` 的语义是"静默成功、失败可见"——正常进度信息不打扰自动化脚本，但出问题时用户必须能看到原因。ST 测试正是依赖这一点：`--quiet` 安装失败时 stdout 里仍有 `[ERROR]` 可供断言。

### 4.2 version.cmake 与 .run 包生成链路

#### 4.2.1 概念说明

`version.cmake` 只有 17 行，却是整个包的"版本锚点"：包名 + 版本号 + 与其他 CANN 包的依赖约束。根 `CMakeLists.txt` 在配置期 `include(version.cmake)`，cann-cmake 的函数据此生成 `version.oam-tools.info` 文件，最终改名成包内的 `version.info`。

#### 4.2.2 核心流程

从源码到 `build_out/cann-oam-tools_9.1.0_linux-<arch>.run` 的链路：

```
build.sh
  └─ cmake ..                      # 配置期
       ├─ include(version.cmake)   # set_cann_package(oam-tools VERSION "9.1.0")
       ├─ check_cann_pkg_build_deps / add_cann_version_info_targets
       │    └─ 生成 build/version.oam-tools.info
       ├─ install(...) 规则登记各组件文件（COMPONENT oam-tools）
       └─ include(cmake/package.cmake) → pack_built_in()
            ├─ install 脚本目录 + cann-cmake 安装公共库到组件
            ├─ install version.oam-tools.info → RENAME version.info
            └─ set_cann_cpack_config(...)     # 配 CPack/makeself 后端
  └─ make package                  # CPack 收集组件 → makeself 打 .run
  └─ mv cann*.run build_out/       # 搬运产物
```

#### 4.2.3 源码精读

版本锚点本体：

[version.cmake:L11-L17](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/version.cmake#L11-L17)

`set_cann_package(oam-tools VERSION "9.1.0")` 定义包名与版本；两对 `set_cann_build_dependencies` / `set_cann_run_dependencies` 声明编译期与运行期对 runtime、metadef 的最低版本要求（>=9.0）——安装器的 `preinstall_process` 版本兼容检查（见 4.3.3）用的就是这套约束生成的数据。

根 CMakeLists 定义 staging 目录并触发打包：

[CMakeLists.txt:L308-L310](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L308-L310)

[CMakeLists.txt:L32-L38](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L32-L38) 先定义了 `OAM_STAGING_DIR`（makeself 暂存目录），并按包类型切换 `INSTALL_LIBRARY_DIR`：run 包用绝对 staging 路径，rpm/deb 交由 `CPACK_PACKAGING_INSTALL_PREFIX` 收集。

`pack_built_in()` 里两处关键 install：脚本族进包与 version.info 改名：

[cmake/package.cmake:L109-L130](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/package.cmake#L109-L130)

注意 L118-L122：`install(FILES ${CMAKE_BINARY_DIR}/version.oam-tools.info ... RENAME version.info)`——构建期生成的版本文件在打包时改名为安装器认识的 `version.info`。L127-L130 把 cann-cmake 的 `multi_version.inc`、`common_func_v2.inc` 等安装公共库也塞进 `share/info/oam_tools/script`，这些 `.inc` 文件正是 `install.sh` 开头 source 的那些依赖。

最后一行启动 CPack 配置：

[cmake/package.cmake:L144-L144](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/package.cmake#L144-L144)

`set_cann_cpack_config(oam-tools SHARE_INFO_NAME oam_tools PACKAGE_TYPE ${PACKAGE_TYPE})` 是 cann-cmake 提供的函数，它按 `oam_tools.xml` 的描述配置 CPack（run 类型走 makeself 生成器），产出文件名形如 `cann-oam-tools_9.1.0_linux-aarch64.run`。

build.sh 侧的驱动与产物搬运：

[build.sh:L306-L321](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L306-L321)

打包前先 `rm -f cann*.run` 清历史产物（避免把上一次的旧包误当本次产物），`make package` 后只按本次 `PACKAGE_TYPE` 后缀匹配搬运到 `build_out/`，找不到产物即报错返回。

#### 4.2.4 代码实践

**实践目标**：亲手追出 `version.info` 内容的来源。

**操作步骤**：

1. 读 [version.cmake:L11](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/version.cmake#L11)，记下版本号。
2. 执行 `bash build.sh`（或在已有构建机上直接看），构建完成后查看 `build/version.oam-tools.info` 文件内容（若没有完整构建环境，跳到第 3 步做纸面推导）。
3. 在仓库里 grep `version.oam-tools.info`，确认它唯一的消费点在 `cmake/package.cmake` 的 RENAME 处。

**需要观察的现象**：`version.oam-tools.info` 里的 `Version=` 值与 `version.cmake` 声明一致；该文件被打包改名后，成为安装器 `--version` 参数与 `getrunpkginfo()` 的数据源。

**预期结果**：能画出"version.cmake 声明 → 构建期生成 .info → 打包改名 version.info → 安装器读取"的数据流。实际构建产物内容**待本地验证**（需要完整 CANN 编译环境）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `version.cmake` 里的 VERSION 改成 `9.2.0`，包名会变吗？

**答案**：会。包名由 `set_cann_cpack_config` 按 `set_cann_package` 登记的名字与版本拼出，产物是 `cann-oam-tools_9.2.0_linux-<arch>.run`。ST 的 `run_package` fixture 用 `cann-oam-tools_*.run` 通配符找包（见 4.4.3），所以 fixture 不受版本号变化影响。

**练习 2**：为什么 `INSTALL_LIBRARY_DIR` 要按 PACKAGE_TYPE 切换？

**答案**：run 包（makeself）需要文件先落到绝对路径的 staging 目录再整体打包；rpm/deb 则由 CPack 的 `CPACK_PACKAGING_INSTALL_PREFIX` 机制自己处理安装前缀，给相对路径即可。同一份 install 规则要服务三种包类型，所以在配置期分叉。

### 4.3 install.sh：安装/升级/卸载的统一入口

#### 4.3.1 概念说明

`install.sh`（约 1700 行）是 .run 包解压后第一个被执行的脚本，它解决的问题是：**用一个入口统一三种生命周期操作**（安装/升级/卸载），并前置所有"环境合法性"检查（架构、权限、已装版本、路径）。真正的文件搬运则委托给 `opp_install.sh` / `opp_upgrade.sh` / `opp_uninstall.sh`（它们又委托 cann-cmake 的 `install_common_parser.sh`）。

#### 4.3.2 核心流程

install.sh 主流程伪代码：

```
读取 scene.info 的 arch → 与本机 uname -m 比对
解析参数（--full/--run/--devel/--upgrade/--uninstall/--quiet/--install-path/--chip/...）
  └─ iter_i 计数器保证"三种操作类型只能选一个"
确定 target_dir（默认 /usr/local/Ascend/cann，多版本包走 share/info 子路径）
初始化日志文件（/var/log/ascend_seclog/{ascend_install.log, operation.log}）
读取已安装信息（ascend_install.info）与新包版本（version.info）→ 打印对比

if is_install:                      # --full/--run/--devel
    校验用户/组、目录权限
    precleanbeforeinstall           # 同路径重装时先清旧安装
    preinstall_process              # 版本兼容性检查（runtime/metadef 约束）
    sh opp_install.sh ...           # 拷贝全部文件 + 收紧权限
    若存在 msprof wheel → bash msprof_install.sh
elif is_upgrade:                    # --upgrade
    preinstall_process
    sh opp_upgrade.sh ...
    收紧 script 目录权限为 555/550
elif is_uninstall:                  # --uninstall
    sh opp_uninstall.sh ...
logoperationretstatus               # 写 operation.log 并决定退出码
```

#### 4.3.3 源码精读

参数解析的"单选约束"靠 `iter_i` 计数器实现：

[scripts/package/oam_tools/scripts/install.sh:L1129-L1144](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/scripts/install.sh#L1129-L1144)

`--run`/`--full`/`--devel` 只置安装类型并 `is_install=y`，`--upgrade`、`--uninstall` 各自置标志；三者都会让 `iter_i` 加 1。随后在 [install.sh:L1250-L1255](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/scripts/install.sh#L1250-L1255) 检查 `iter_i != 1` 即报错——这就是"只能选一种操作类型"的实现。`--install-path`（[L1145-L1152](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/scripts/install.sh#L1145-L1152)）则做路径合法性与空格检查后记录目标路径。

架构一致性检查：

[scripts/package/oam_tools/scripts/install.sh:L1229-L1236](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/scripts/install.sh#L1229-L1236)

`platform_data` 来自打包期生成的 `scene.info` 的 `arch=` 行（见 4.1.3 的 generate_info）。x86 机器上装 aarch64 包会在这里被拒绝。

安装分支的核心调用：

[scripts/package/oam_tools/scripts/install.sh:L1630-L1648](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/scripts/install.sh#L1630-L1648)

先 `preinstall_process` 做版本兼容检查（消费 version.cmake 声明的依赖约束），失败直接退出；再以十余个位置参数调 `opp_install.sh` 完成真正的文件释放；最后如果包里有 msprof wheel（路径 `tools/profiler/profiler_tool/msprof-0.0.1-py3-none-any.whl`，正是 u4-l1 讲过的 profiler_tool 流水线产物），追加执行 `msprof_install.sh` 解包。升级分支（[L1654-L1687](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/scripts/install.sh#L1654-L1687)）与卸载分支（[L1689-L1715](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/scripts/install.sh#L1689-L1715)）结构对称，分别转调 `opp_upgrade.sh` 与 `opp_uninstall.sh`。

`opp_install.sh` 的文件搬运其实是一行委托：

[scripts/package/oam_tools/scripts/opp_install.sh:L436-L437](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/scripts/opp_install.sh#L436-L437)

`install_common_parser.sh --copy_all` 是 cann-cmake 的通用安装器，按 `filelist.csv`（打包期由 XML 生成的文件清单）逐条拷贝并设权限。它前面 L421-L434 的"Pre-chmod"很有意思：上一次安装会把目录锁成 555 只读，`cp -af` 写不进去，所以先用 `find ... ! -perm -u+w -exec chmod u+w` 解锁。拷贝完成后（文件尾部）`chmod 444 ascend_install.info` 把安装状态文件锁成只读，防止用户手改。

安装后的"用户卸载入口"是 `uninstall.sh`，它的实现是反向转调：

[scripts/package/oam_tools/scripts/uninstall.sh:L76-L83](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/scripts/uninstall.sh#L76-L79)

它从自身位置反推出安装根路径（上跳四级），再以 `--uninstall --install-path=...` 转调同目录的 `install.sh`（前两个 `--aa` 占位参数是给 install.sh 开头"剥掉 .run 自身参数"的 while 循环用的）。这个脚本被释放为 `<安装根>/cann/cann_uninstall.sh`——ST 卸载测试的 Method B 测的就是它。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `--full` 安装的完整调用链并记录每步日志。

**操作步骤**：

1. 在任意 Linux 机器（无需 NPU，安装器本身不碰设备）上构建或获取 `cann-oam-tools_*.run`。
2. 执行 `bash cann-oam-tools_*_linux-<arch>.run --full --install-path=$HOME/test_install`。
3. 观察终端输出中的 `upgradePercentage` 进度行与结尾的安装信息汇总。
4. 查看 `/var/log/ascend_seclog/ascend_install.log`（root）或 `$HOME/var/log/ascend_seclog/ascend_install.log`（普通用户），对照 4.3.2 的伪代码逐行核对。
5. 执行 `bash cann-oam-tools_*_linux-<arch>.run --version` 与 `--full --upgrade` 各一次，观察版本读取与升级路径的差异。

**需要观察的现象**：安装日志按"startlog → 架构检查 → 兼容性检查 → copy_all → 权限收紧 → logoperationretstatus"顺序推进；`cann_uninstall.sh` 出现在 `$HOME/test_install/cann/` 下。

**预期结果**：得到一份与伪代码对应的真实日志时间线。**待本地验证**（本环境未构建 .run 包）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `install.sh` 把 install/upgrade/uninstall 三种操作放在一个脚本里，而不是三个脚本？

**答案**：三种操作共享大量前置逻辑（参数解析、路径检查、`ascend_install.info` 读取、日志初始化、权限检查），合并可以避免三份拷贝漂移；且 `uninstall.sh`（cann_uninstall.sh）可以简单地向同目录的 `install.sh` 传 `--uninstall` 复用整条链路。

**练习 2**：`logoperationretstatus`（[install.sh:L149-L184](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam-tools/../oam_tools/scripts/install.sh#L149-L184)）为什么给 install/upgrade/uninstall 分别标 SUGGESTION/MINOR/MAJOR 级别？

**答案**：这是写入 `operation.log` 的审计事件级别：全新安装是建议级记录；升级对系统是次要变更；卸载移除文件、影响最大，记 MAJOR 级别便于事后审计追溯。

**练习 3**：`precleanbeforeinstall`（[install.sh:L280-L405](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/scripts/install.sh#L280-L405) 在同路径重复安装时做什么？

**答案**：先检测安装目录已有文件并交互确认（quiet 模式自动 y）；若检测到同路径已装过 oam-tools（`_installed_path` 等于目标路径），则先调用旧安装自带的 `uninstall.sh` 清掉旧安装，再继续新安装——这就是 upgrade ST 里"同目录二次 --full 能成功"的机制保障。

### 4.4 test/st 三件套：install / upgrade / uninstall ST

#### 4.4.1 概念说明

三组 ST 是 .run 包的"验收测试"：不 import 项目源码、不 mock 任何东西，把包当黑盒命令跑，只断言三件事——退出码为 0、输出无 `[ERROR]`、磁盘产物符合预期。它们回答的问题是："这个包在干净机器上装得上、盖得掉（升级）、删得净（卸载）吗？"

#### 4.4.2 核心流程

三组测试共享的骨架（fixtures）：

```
run_package（session 级）
  └─ 在 build_out/ 找 cann-oam-tools_*.run，没有则整 session skip
install_dir（函数级）
  └─ 在 build_out/ 下建 0755 的临时安装根（chmod 755 是为了绕过安装器
     对父目录权限的 755 检查），测试后 chmod -R u+w 再删
installed_dir（仅 uninstall，继承上两者）
  └─ 先 --full 静默装好，yield 给测试去卸载
```

#### 4.4.3 源码精读

fixture 的找包与跳过逻辑：

[test/st/conftest.py:L34-L49](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/conftest.py#L34-L49)

没有 .run 包时 `pytest.skip` 整个 session 并提示先 `bash build.sh`——这呼应 u6-l1 讲过的"无包自动打包或跳过"策略（run_tests.sh 侧也会在打 ST 前自动触发打包）。

[test/st/conftest.py:L52-L68](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/conftest.py#L52-L68)

`install_dir` 的 docstring 解释了一个真实踩坑：安装器（root 下）拒绝权限低于 0755 的祖先目录，而 pytest 的 `tmp_path` 与 `mkdtemp` 默认都是 0700，所以必须显式 chmod 0755。收尾时先 `chmod -R u+w` 解锁安装器设的只读位，否则 `rmtree` 会失败。

安装 ST 的主断言：

[test/st/install/testcase/test_install_st.py:L66-L76](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/install/testcase/test_install_st.py#L66-L76)

参数化覆盖 `--full/--run/--devel` 三种安装类型，只断言退出码与无 `[ERROR]`。文件头 docstring（L27-L37）专门解释了**为什么不断言 `[WARNING]`**：安装器把"父目录非 root/755""目录已有文件""版本软检查失败"都设计为非致命告警，在完全正常的环境也会打 WARNING——若断言无 WARNING 就会把建议级信息误升为错误，使测试环境脆弱。这是对安装器契约的精准理解。

产物存在性检查：

[test/st/install/testcase/test_install_st.py:L81-L98](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/install/testcase/test_install_st.py#L81-L98)

`--full` 后必须存在四个"锚点产物"：`cann/share/info/oam_tools/`（安装信息目录）、其中的 `ascend_install.info`、`<arch>-linux/include/version/oam_tools_version.h`（由 XML 的 generate_info 生成，见 4.1.3）、`cann/cann_uninstall.sh`（即 4.3.3 的 uninstall.sh 释放位）。

最有设计感的是解压/安装一致性看护：

[test/st/install/testcase/test_install_st.py:L142-L181](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/install/testcase/test_install_st.py#L142-L181)

`--noexec --extract` 只解压不安装，得到包的 staging 目录树；测试断言"解压树是安装树的子集"——任何出现在解压目录却没被安装出来的文件都意味着安装器丢文件。L184-L224 的第二个用例进一步对 `tools/profiler/profiler_tool` 子树做**双向精确比对**（排除 `__pycache__`，因为它是安装期 msprof_install.sh 用目标机 Python 现场生成的，构建期已清理）。

升级 ST 的场景矩阵：

[test/st/upgrade/testcase/test_upgrade_st.py:L55-L79](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/upgrade/testcase/test_upgrade_st.py#L55-L79)

4 个场景覆盖"同类型重装"（full→full）与"类型切换"（run→full、full→run、devel→full）；先装基线再二次安装，只对第二次的退出码与输出做断言。这里测的实际是 4.3.3 练习 3 的 `precleanbeforeinstall` 路径：同目录覆盖安装能否干净完成。

卸载 ST 的残留检查：

[test/st/uninstall/testcase/test_uninstall_st.py:L52-L72](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/uninstall/testcase/test_uninstall_st.py#L52-L72)

`_collect_residuals` 列出卸载后**不允许残留**的 oam-tools 专属路径：安装信息目录、`include/aclnnop/`、版本头文件、`lib64/libopapi_oam.so`（对应 XML 的 aml_lib / ascend_dump_parser_lib 交付物）。两种卸载方法各一个用例：Method A 直接 `xxx.run --uninstall`；Method B 执行安装释放的 `cann/cann_uninstall.sh`（[L107-L140](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/uninstall/testcase/test_uninstall_st.py#L107-L140)）。L336 附近 opp_uninstall.sh 注释还提到曾出现"cann_uninstall.sh 卸载后残留 libopapi_oam.so 与空目录树"的真实缺陷，残留检查正是为这类回归设防。

ST 的运行入口在 run_tests.sh：

[scripts/run_tests.sh:L636-L643](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/run_tests.sh#L636-L643)

三个 case 名 `install_st` / `upgrade_st` / `uninstall_st` 分别映射到三个 testcase 目录，由 `bash build.sh -u --st --component install|upgrade|uninstall` 驱动（见 u6-l1）。

#### 4.4.4 代码实践（本讲综合实践入口）

**实践目标**：跑通 install ST 的最小闭环。

**操作步骤**：

1. 先构建 .run 包：`bash build.sh`（需要 CANN 编译环境；若不可用，阅读后续步骤做纸面推演）。
2. 确认 `build_out/cann-oam-tools_*_linux-<arch>.run` 存在。
3. 单独跑安装 ST：`bash build.sh -u --st --component install`，或直接 `python3 -m pytest test/st/install/testcase -v`。
4. 观察 `run_package` fixture 是否命中、三个参数化用例与两个一致性用例的通过情况。

**需要观察的现象**：若第 1 步未构建，pytest 输出 `SKIPPED [n] ... No cann-oam-tools*.run found`——skip 而非 fail，这是 fixture 的设计意图。

**预期结果**：5 个 install ST 用例全部 PASSED。**待本地验证**（本环境无构建产物与 CANN 环境）。

#### 4.4.5 小练习与答案

**练习 1**：升级 ST 为什么不断言 `[WARNING]`？

**答案**：升级必然触发 `precleanbeforeinstall` 的"Install folder has files existed"告警，这是设计内行为（test_upgrade_st.py 文件头 docstring 明确说明）。断言无 WARNING 会必然失败。

**练习 2**：卸载 ST 为什么需要独立的 `installed_dir` fixture 而不复用 `install_dir`？

**答案**：卸载测试的前置是"已经装好"（[test/st/uninstall/testcase/conftest.py:L28-L40](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/uninstall/testcase/conftest.py#L28-L40)），该 fixture 在 yield 前先执行一次 `--full --quiet` 安装并断言成功，把"安装"从被测行为降级为前置条件，测试本体只关心卸载。

**练习 3**：`test_extract_subset_of_install` 为什么只断言"解压 ⊆ 安装"这个方向，而不是双向相等？

**答案**：安装期会动态生成解压目录里没有的文件——`oam_tools_version.h`、`__pycache__`、`ascend_install.info` 等，所以安装树必然是超集；反向断言会误报。只有 `profiler_tool` 子树（纯解包产物）才做排除 `__pycache__` 后的双向精确比对。

## 5. 综合实践：梳理一次完整发版链路

在没有设备的环境也能完成（安装器不依赖 NPU；无法真实执行的部分标注纸面推演）。产出一张"发版链路清单"表格，每行包含：步骤、命令/脚本、关键文件、对应验证用例。

参考骨架（请自行补全"验证用例"列）：

| 步骤 | 命令/脚本 | 关键文件 | 对应 ST 用例 |
| --- | --- | --- | --- |
| 1. 打包 | `bash build.sh` → `make package` | version.cmake、cmake/package.cmake、oam_tools.xml | （run_package fixture 前置） |
| 2. 安装 | `xxx.run --full --install-path=...` | install.sh → opp_install.sh → msprof_install.sh | test_install_st.py::TestInstall/TestInstallArtefacts |
| 3. 检查释放 | `ls` 安装根 | tools/hccl_test、tools/profiler、tools/ascend_system_advisor/asys、cann_uninstall.sh | TestExtractInstallConsistency |
| 4. 升级 | `xxx.run --full`（同路径二次） | install.sh → precleanbeforeinstall → opp_upgrade.sh | test_upgrade_st.py::TestUpgrade |
| 5. 卸载 A | `xxx.run --uninstall` | install.sh → opp_uninstall.sh | TestUninstallViaRunPackage |
| 6. 卸载 B | `cann/cann_uninstall.sh` | uninstall.sh 反向转调 | TestUninstallViaCannUninstallSh |

具体要求：

1. 为第 2 步补全 install.sh 内部的调用顺序（架构检查 → 兼容性检查 → copy_all → 权限收紧），每步标注源码行号。
2. 为第 3 步对照 `oam_tools.xml` 的 `file_info`/`dir_info` 列出你期望出现在 `tools/` 下的全部子目录。
3. 有条件时真实执行一遍并把每步的实际退出码记入表格；无条件时标注"纸面推演"。

## 6. 本讲小结

- .run 包的生成链路是：`version.cmake` 声明版本 → 配置期生成 `version.oam-tools.info` → `cmake/package.cmake` 的 `pack_built_in()` 把安装脚本族与版本文件装进 CPack 组件 → `set_cann_cpack_config` 走 makeself 打包 → `build.sh` 把 `cann*.run` 搬到 `build_out/`。
- `scripts/package/oam_tools/oam_tools.xml` 是"装什么、装到哪、什么权限"的声明式单一事实源；四大组件的目标路径（tools/、tools/ascend_system_advisor/asys 等）都由它定义。
- `install.sh` 是三种生命周期操作的统一入口：`iter_i` 计数器保证单选，`precleanbeforeinstall` 支撑同路径覆盖安装，`preinstall_process` 消费 version.cmake 的依赖约束，真正的文件搬运委托给 opp_*.sh 与 cann-cmake 的 `install_common_parser.sh --copy_all`。
- `cann/cann_uninstall.sh` 只是安装释放的 `uninstall.sh`，实现上是从自身位置反推安装根后反向转调 `install.sh --uninstall`。
- 三组 ST 是纯黑盒验收：install 断言退出码 + 无 `[ERROR]` + 锚点产物 + 解压/安装树一致性；upgrade 跑 4 组类型切换场景；uninstall 用两种方法验证并检查专属文件零残留。WARNING 被有意不断言，因为它在设计上是非致命建议。

## 7. 下一步学习建议

下一讲 u6-l4「二次开发实战：为 oam-tools 贡献新功能」将综合 asys 采集框架、msprof 插件机制与 hccl_test 测试框架的扩展点。学完本讲后，建议读者：

1. 通读 `scripts/package/oam_tools/scripts/install.sh` 中未精读的权限检查函数族（`parent_dirs_permission_check`、`checkprefolderspermission`），理解 CANN 包对安装路径安全的强约束。
2. 对照 `test/st/install/testcase/test_install_st.py` 的 docstring 与 `install.sh` 的告警调用点，体会"测试断言必须匹配被测系统契约"这一工程原则。
3. 若要为本仓贡献新的发布物，先在 `oam_tools.xml` 加 `file_info` 条目，再在对应 ST 里加产物存在性断言——描述、安装、验证三处同步更新。
