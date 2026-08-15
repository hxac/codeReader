# hccl_test 总览：构建方式与工程结构

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 hccl_test 在 oam-tools 中的定位——它是一个基于 HCCL（Huawei Collective Communication Library，华为集合通信库）单算子 API 的、用 C++ 编写的集合通信**功能正确性 + 通信性能**测试工具。
2. 理解它「Makefile 负责编译、CMakeLists.txt 负责安装」的双构建入口设计，以及 11 个测试二进制如何从同一份 main 文件产出。
3. 理解 `common/` 与 `opbase_test/` 两个目录的职责划分：公共骨架 vs. 每算子一份的实现。
4. 列出 opbase_test 支持的全部 11 种集合通信算子及其对应的测试源文件。
5. 理解 hostfile 文件在多机（多节点）测试场景中的作用与格式。

本讲是 hccl_test 单元（u5）的第一讲，只搭骨架；`HcclTest`/`HcclOpBaseTest` 基类的测试骨架精读留给 u5-l2，运行与结果解读留给 u5-l3。

## 2. 前置知识

阅读本讲前，建议先了解以下概念（不熟悉也没关系，下面用通俗语言解释）：

- **集合通信（Collective Communication）**：分布式训练中，多张加速卡之间经常需要「全员参与」的数据交换，例如把每张卡上的梯度**加起来**（AllReduce）、把每张卡的数据**拼起来**（AllGather）、把一份数据**发给所有人**（Broadcast）。HCCL 就是昇腾平台上这类操作的库实现，对标 NVIDIA 生态的 NCCL。
- **rank（进程号）**：集合通信里每个参与者的编号。`mpirun -n 8` 拉起 8 个进程，每个进程持有一张 NPU，rank 从 0 编号到 7。
- **MPI**：Message Passing Interface，经典的并行进程管理与通信标准。hccl_test 用 MPI 做两件事：① 用 `mpirun` 把测试进程拉起到多台机器上；② 在构建 HCCL 通信域前，用 MPI 广播（`MPI_Ibcast`）同步各 rank 的通信根信息（详见 u5-l2）。
- **rootinfo**：opbase_test 目录下所有文件名里都有 `rootinfo` 一词，指的是 `HcclGetRootInfo` 接口获取的「通信域根信息」——HCCL 建立通信域前需要 root rank 生成这份信息并广播给其他 rank。
- **正确性 vs. 性能**：hccl_test 每个算子二进制都能做两种验证——`-c 1` 开启逐元素比对校验（正确性），默认则输出各数据量档位的耗时与算法带宽（性能）。
- **构建前置**：本讲依赖 u1-l2 讲过的 oam-tools 构建体系（build.sh → CMake → .run 包）。hccl_test 是其中唯一的「Makefile + CMake 双入口」组件。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/hccl_test/README.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/README.md) | 组件自述：目录结构、三层类继承体系、11 个算子的能力矩阵、使用示例 |
| [src/hccl_test/Makefile](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile) | 独立编译入口：编译选项、库链接、11 个目标到源文件的映射规则 |
| [src/hccl_test/CMakeLists.txt](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/CMakeLists.txt) | 安装入口：把整个 hccl_test 目录装进 .run 包的 `tools/` 下，不参与编译 |
| [src/hccl_test/common/src/hccl_test_main.cc](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_main.cc) | 全部 11 个二进制共享的唯一 `main()`，编排初始化 → 测试 → 清理全流程 |
| [src/hccl_test/common/src/hccl_test_common.h](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.h) | `HcclTest` 基类声明与工厂函数 `init_opbase_ptr` / `delete_opbase_ptr` 声明 |
| [src/hccl_test/hostfile](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/hostfile) | 多机节点清单模板文件（仅一行注释） |
| [docs/zh/hccl_test/execution.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md) | 用户文档：hostfile 格式与 mpirun 启动方式 |
| [docs/zh/hccl_test/cmdline_options_desc.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/cmdline_options_desc.md) | 用户文档：mpirun 与测试二进制的命令行参数说明 |

目录组织一览（见 [README.md:L8-L34](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/README.md#L8-L34)）：

```text
hccl_test/
├── CMakeLists.txt            # 安装规则（打进 .run 包）
├── Makefile                  # 编译规则（产出 11 个二进制到 bin/）
├── hostfile                  # 多机节点清单模板
├── common/src/               # 公共骨架：main、HcclTest 基类、校验、内存初始化
└── opbase_test/              # 每个集合通信算子一对 .h/.cc
```

## 4. 核心概念与源码讲解

### 4.1 Makefile 与 CMakeLists：双构建入口的分工

#### 4.1.1 概念说明

回顾 u1-l2：oam-tools 整仓由 build.sh 驱动根 CMakeLists.txt 统一构建打包。但 hccl_test 是个例外——它**不用 CMake 编译代码**，而是保留了一份传统的 GNU Makefile。这样就形成了两个入口：

| 入口 | 触发方式 | 做什么 | 产物 |
|------|---------|--------|------|
| Makefile | 用户装好 CANN 后手动 `make`（或由 .run 包安装后的目录内执行） | 编译 11 个算子测试二进制 | `bin/<算子>_test` 共 11 个可执行文件 |
| CMakeLists.txt | 仓级 `build.sh` → 根 CMake → `add_subdirectory(hccl_test)` | 把 hccl_test **整个目录原样安装** | .run 包中 `${INSTALL_LIBRARY_DIR}/tools/hccl_test/` |

为什么这样设计？因为编译 hccl_test 需要两样「装机后才确定」的外部依赖：CANN Toolkit 的头文件和库（HCCL/ACL/Msprofiler），以及 MPI。这两者的路径因机器而异，让用户在目标机上用 `make MPI_HOME=... ASCEND_DIR=...` 现场编译，比在打包机上写死交叉环境简单得多。CMake 侧只负责「把源码运到用户机器上」。

#### 4.1.2 核心流程

Makefile 的执行逻辑可以概括为四步：

1. **定编译选项**：`CXXFLAGS` 定死 C++11 与一组安全加固选项（`-fstack-protector-strong`、`-fPIE -pie`、`-Wl,-z,relro/-z,now/-z,noexecstack` 等）；若 make 目标行里出现 `HCCL_TEST_LOG_ENABLE`，追加 `-DHCCL_TEST_LOG_ENABLE` 宏打开日志。
2. **定依赖路径**：从环境变量式赋值 `ASCEND_DIR`（CANN 安装路径）与 `MPI_HOME` 推导头文件/库目录。
3. **定目标清单与源映射**：`LIST` 列出 11 个目标名；每个目标用「目标专属变量」`SRC = xxx.cc` 绑定 opbase_test 里对应算子的实现文件。
4. **模式化编译**：对 `LIST` 中任一目标，统一执行 `g++ $(CXXFLAGS) $(Common_SRC) $(Opbase_DIR)/$(SRC) ... -o bin/$@ $(LIBS)`——即「全部公共源码 + 该算子一个 .cc」链接成一个二进制。

#### 4.1.3 源码精读

**编译选项与日志宏开关**。[Makefile:L4-L18](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L4-L18)：`CXXFLAGS` 以 `-Werror`（警告即错误）和一串 RELRO/PIE/栈保护安全选项开头；第 16-18 行用 `MAKECMDGOALS`（make 命令行上写的目标列表）判断用户是否敲了 `make HCCL_TEST_LOG_ENABLE ...`，是则定义同名宏，供源码里的 `HCCL_TEST_LOG()` 打印调试日志（在 4.2 的 main 里会看到它）。

**目录与外部依赖变量**。[Makefile:L20-L35](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L20-L35)：公共源码用 `wildcard` 全量收集 `common/src/*.cc`；HCCL、ACL、MPI、Msprofiler 四类依赖的头文件/库目录分别由 `ASCEND_DIR` 和 `MPI_HOME` 两个变量拼出。用户编译时必须传入这两个变量（README 的用法：`make MPI_HOME=/path/to/mpi ASCEND_DIR=${ASCEND_HOME_PATH}`）。

**11 个目标及其源文件映射**。[Makefile:L37-L66](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L37-L66)：`LIST` 定义 11 个目标名；随后每个目标一行 `目标: SRC = 源文件`，这是 GNU make 的「目标专属变量」语法——只有构建该目标时 `SRC` 才取这个值。注意 `broadcast_test` 对应的源文件拼写是 `hccl_brocast_rootinfo_test.cc`（历史上少了个 ad，仓库内保持了这个拼写）。

**链接库与编译规则**。[Makefile:L42-L45](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L42-L45) 的 `LIBS` 链接了 4 个库：

- `-lhccl`：HCCL 集合通信库（被测对象，提供 `HcclAllReduce` 等单算子 API）；
- `-lacl_rt`：ACL 运行时库（设备管理、内存分配、Event 计时）；
- `-lmpi`：MPI 库（进程拉起与通信域根信息广播）；
- `-lmsprofiler`：Msprofiler 性能采集库。

[Makefile:L79-L82](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L79-L82) 是统一的模式规则：先 `mkdir -p bin`，然后一条 g++ 命令把「全部公共源码 + 该算子唯一的 `.cc`」编成 `bin/$@`，成功后打印绿色的 "compile completed"。这条规则就是「一份 main + 每算子一个实现 = 一个二进制」的落点。

**CMakeLists 只做安装**。[CMakeLists.txt:L17-L27](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/CMakeLists.txt#L17-L27)：全文没有 `add_executable`/`add_library`，只有一条 `install(DIRECTORY ...)`，把整个 hccl_test 目录（连同 Makefile、hostfile、源码）拷贝到 `.run` 包的 `tools/` 下，组件标记为 `oam-tools`。这印证了 4.1.1 的分工：**CMake 是搬运工，Make 是编译器**。

#### 4.1.4 代码实践

**实践：读懂一次 `make` 到底做了什么**

1. **实践目标**：不实际编译（需要昇腾环境），通过阅读 Makefile 推导出 `make all_reduce_test` 这条命令展开后的完整 g++ 命令行。
2. **操作步骤**：
   1. 打开 [Makefile:L4-L13](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L4-L13)，抄下 `CXXFLAGS` 的全部选项；
   2. 查看 [Makefile:L23](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L23)，`Common_SRC` 会展开为 `common/src/` 下全部 9 个 `.cc` 中参与编译的源文件（其中 `.h` 不算源文件，共 5 个 `.cc`）；
   3. 查 [Makefile:L58](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L58) 得到 `SRC = hccl_allreduce_rootinfo_test.cc`；
   4. 把这些代入 [Makefile:L81](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L81) 的命令模板，手写出完整命令；
   5. 若有 Linux 环境，可在任意含 `.cc` 的目录验证 make 展开：`make -n` 只打印命令不执行（本仓需先设好 `ASCEND_DIR`/`MPI_HOME` 变量，否则 `-I` 参数为空，但 `-n` 仍能打印出命令骨架）。
3. **需要观察的现象**：`make -n` 打印出的命令里，公共源码部分对 11 个目标完全相同，唯一变化的是 `$(Opbase_DIR)/$(SRC)` 这一段。
4. **预期结果**：你能写出类似 `g++ -std=c++11 -Werror ... ./common/src/hccl_test_main.cc ./common/src/hccl_test_common.cc ... ./opbase_test/hccl_allreduce_rootinfo_test.cc -I... -o bin/all_reduce_test -L... -lhccl -lacl_rt -lmpi -lmsprofiler` 的完整命令。**待本地验证**（`make -n` 的输出以本机 make 版本为准）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 hccl_test 不像 asys、msaicerr 那样在打包机上由 build.sh 直接编译好？

**参考答案**：因为编译期需要两样目标机上才有的外部依赖——CANN Toolkit（HCCL/ACL/Msprofiler 的头文件和 so）与 MPI，且路径由用户环境决定（`ASCEND_DIR`、`MPI_HOME`）。asys/msaicerr 是纯 Python 无需编译；hccl_test 选择「.run 包只搬运源码，用户现场 make」，用 [CMakeLists.txt:L23-L27](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/CMakeLists.txt#L23-L27) 的 install 规则实现搬运。

**练习 2**：`make HCCL_TEST_LOG_ENABLE all` 和 `make all` 的产物有什么区别？

**参考答案**：二进制文件名和个数相同（都是 11 个），但前者在 `CXXFLAGS` 里多了 `-DHCCL_TEST_LOG_ENABLE` 宏（[Makefile:L16-L18](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L16-L18)），编译出的程序会把 main 中 `HCCL_TEST_LOG()` 的耗时日志打印出来。这也是为什么 [Makefile:L72-L73](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L72-L73) 要把它声明成 `.PHONY` 目标并依赖 `all`——它本身不是文件，只是一个「开关目标」。

**练习 3**：如果新增第 12 个算子测试，Makefile 需要改哪几行？

**参考答案**：改两处即可——① [Makefile:L37](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L37) 的 `LIST` 追加目标名 `xxx_test`；② 仿照 [Makefile:L56-L66](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L56-L66) 加一行 `xxx_test: SRC = hccl_xxx_rootinfo_test.cc`。模式规则会自动覆盖新目标。

### 4.2 hccl_test_main.cc：一份 main，十一个二进制

#### 4.2.1 概念说明

hccl_test 最有意思的工程决策是：**全部 11 个测试二进制共享同一份 `main()`**（`common/src/hccl_test_main.cc`），每个算子只贡献一个实现类。main 通过「工厂函数 + 链接期决定实现」的多态手法，让同一份入口代码在不同二进制里实例化不同的算子测试类：

- `hccl_test_main.cc` 里只调用全局函数 `init_opbase_ptr()` / `delete_opbase_ptr()`；
- 这两个函数**在每个算子的 `.cc` 里各有一份定义**（通过链接进来的那一个 `.cc` 决定行为）；
- `all_reduce_test` 这个二进制链接的是 `hccl_allreduce_rootinfo_test.cc`，所以它的 `init_opbase_ptr` 返回 `HcclOpBaseAllreduceTest` 实例。

这正是 README「工厂模式与多二进制架构」一节（[README.md:L88-L104](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/README.md#L88-L104)）描述的机制，也和 u2-l1 里 asys 的 `EXECUTE_CMD_FUNC` 字典分发、u2-l4 的芯片 handler 注册异曲同工——**入口不变，实现在别处插拔**。

#### 4.2.2 核心流程

main 的执行流程是一条严格的「初始化 → 测试 → 清理」流水线，用 `goto` 标签实现分级回退（出错越早，清理越少）：

```text
MPI_Init
  └─ init_opbase_ptr()        创建算子测试实例（链接期决定是哪个算子）
      └─ parse_cmd_line()     解析命令行（--help 则直接退出）
          └─ get_mpi_proc()   发现本 host 上的 MPI 进程布局
              └─ check_cmd_line()  校验参数合法性
                  └─ device_init()  ACL 设备/上下文初始化
                      └─ get_env_resource() / set_env_resource()  读写环境变量
                          └─ start_test()   进入测试主流程（通信域构建 + 按数据量遍历）
hccltesterr1:
  release_env_resource()      释放环境变量资源
hccltesterr2:
  delete_opbase_ptr()         删除测试实例
hccltesterr3:
  aclFinalize() + MPI_Finalize()
```

`start_test()` 之后的细节（通信域构建、按 min_bytes→max_bytes 遍历、warmup + 计时 + 校验）属于 u5-l2 的内容，本讲只需要知道「main 把控制权交给 `HcclTest` 基类接口」。

#### 4.2.3 源码精读

**工厂函数声明**。[hccl_test_common.h:L243-L244](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.h#L243-L244)：在 `HcclTest` 基类声明（[hccl_test_common.h:L118](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.h#L118)）之后，声明了 `init_opbase_ptr` / `delete_opbase_ptr` 两个全局函数——main 只认这份声明，具体实现由链接进来的算子 `.cc` 提供。

**main 开头：MPI 初始化与计时日志**。[hccl_test_main.cc:L26-L43](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_main.cc#L26-L43)：`setlinebuf(stdout)` 保证多进程输出即时可见（11 个二进制由 mpirun 并发拉起，不刷缓冲会看不到进度）；接着 `MPI_Init` 并用 `system_clock` 测耗时，通过 `HCCL_TEST_LOG` 输出——这个宏只有在 4.1 讲的 `make HCCL_TEST_LOG_ENABLE` 编译时才有实际输出。随后调用 `init_opbase_ptr` 创建测试实例，空指针则跳到最外层错误标签。

**参数解析与进程发现**。[hccl_test_main.cc:L45-L72](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_main.cc#L45-L72)：`parse_cmd_line` 返回 1 表示用户敲了 `--help`（按成功退出），-1 表示解析失败；`get_mpi_proc` 查明本 host 上被 mpirun 拉起了哪些进程（多机场景下每个节点跑多个 rank）；`check_cmd_line` 做参数合法性终审（如 `-p` 卡数与实际进程数的匹配）。

**设备初始化、环境变量与测试入口**。[hccl_test_main.cc:L74-L99](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_main.cc#L74-L99)：`device_init` 完成 ACL 设备与 Context 初始化；`get_env_resource`/`set_env_resource` 读取并设置测试相关环境变量；最后 `start_test()` 进入测试主流程，同样用 `HCCL_TEST_LOG` 记录总耗时。

**分级清理**。[hccl_test_main.cc:L101-L116](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_main.cc#L101-L116)：三个 `goto` 标签构成清理漏斗——`hccltesterr1` 释放环境资源、`hccltesterr2` 删除测试实例、`hccltesterr3` 执行 `aclFinalize` 与 `MPI_Finalize`。注意正常路径也会顺序落到这些标签（不是出错专用），所以清理代码只有一份。main 的返回值就是进程退出码，mpirun 据此判断任一 rank 失败即整体失败。

#### 4.2.4 代码实践

**实践：用 grep 验证「一份 main、多份工厂实现」**

1. **实践目标**：确认 `init_opbase_ptr` 在仓库里有且仅有一份声明（头文件）、11 份定义（每个算子 `.cc` 一份），并观察不同算子 new 出的类名不同。
2. **操作步骤**：
   1. 在仓库根目录执行 `grep -rn "init_opbase_ptr" src/hccl_test/`；
   2. 统计命中文件：应为 1 个头文件（声明）+ 11 个 `opbase_test/*.cc`（定义）+ 1 个 `hccl_test_main.cc`（调用）；
   3. 再执行 `grep -n "new HcclOpBase" src/hccl_test/opbase_test/*.cc`，对比不同文件里 new 的类名。
3. **需要观察的现象**：每个算子 `.cc` 里 `init_opbase_ptr` 的函数体只有一行实质差异——`new` 的具体类名（如 `HcclOpBaseAllreduceTest` vs `HcclOpBaseBrocastTest`）。
4. **预期结果**：与 [README.md:L92-L102](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/README.md#L92-L102) 给出的 allreduce 示例代码一致。此实践纯 grep，无需昇腾环境，可直接完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么不把 11 个算子编成一个二进制、用命令行参数选择算子？

**参考答案**：拆成 11 个二进制后，每个二进制里 `init_opbase_ptr` 链接的是确定的一个实现，「是哪个算子」在链接期就定死了，main 与公共骨架完全不用 if-else 区分算子；同时运维上很直观——`all_reduce_test` 这个文件名即测试语义，mpirun 命令、FAQ 里的 pkill 清理命令（[faqs.md:L88](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/faqs.md#L88)）都直接引用二进制名。代价是编译 11 次，但这点开销可忽略。

**练习 2**：main 里 `parse_cmd_line` 返回 1 时为什么把 `ret` 置 0 再跳转？

**参考答案**：返回 1 的语义是「用户敲了 `--help`，参数已打印完毕」（见 [hccl_test_main.cc:L47-L50](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_main.cc#L47-L50) 注释），这是正常退出路径，不应让 mpirun 误判为测试失败，所以把返回值归零后再走清理标签。

**练习 3**：`hccltesterr3` 标签里的 `aclFinalize()` 对从未执行 `device_init` 的失败路径（例如 `init_opbase_ptr` 返回空）是否安全？

**参考答案**：从源码看，`hccl_test_main.cc` 对 `aclFinalize` 使用了 `ACLCHECK` 宏包装（[hccl_test_main.cc:L112](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_main.cc#L112)），正常调用要求 ACL 已初始化；`init_opbase_ptr` 失败路径是否允许直接走到该标签，取决于 `ACLCHECK` 宏对错误码的处理方式（宏定义在 hccl_test_common.h 中，本讲未展开）。**待确认**——建议读者在 u5-l2 精读 `ACLCHECK` 宏定义时回头验证这一点。

### 4.3 opbase_test 目录：11 种集合通信算子清单

#### 4.3.1 概念说明

`opbase_test/` 目录下每个集合通信算子一对 `.h/.cc` 文件，命名统一为 `hccl_<算子名小写>_rootinfo_test.h/cc`。这些算子覆盖了分布式训练最常用的通信原语，可按语义分四族：

- **归约族**（多卡数据按某种操作合成）：AllReduce、Reduce、ReduceScatter、ReduceScatterV；
- **聚集族**（多卡数据拼接收集）：AllGather、AllGatherV、Gather 的变体 Scatter（散射，一份数据拆给各卡）；
- **全交换族**（卡卡之间点对点互换）：AlltoAll、AlltoAllV、AlltoAllVC；
- **广播族**：Broadcast。

带 `V` 后缀的（AllGatherV、ReduceScatterV、AlltoAllV）表示**变长**版本——每个 rank 收发的数据量可以不同；`AlltoAllVC` 是 AlltoAllV 再加块对齐（block size 取 2 的幂）的变体。

#### 4.3.2 核心流程

算子测试类全部继承自中间基类 `HcclOpBaseTest`（`HcclOpBaseTest` 又继承 `HcclTest`，三层体系的精读在 u5-l2）。本讲只需记住叶节点的两个职责（见 [README.md:L84-L86](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/README.md#L84-L86)）：

1. 覆写 `hccl_op_base_test()`：写「预热 → Event 计时 → 迭代调用 HCCL API → 可选校验」的具体算子逻辑；
2. 覆写 `init_malloc_Ksize_by_data()` / `init_send_recv_size_by_data()` 等：定义该算子特有的 send/recv 内存布局。

#### 4.3.3 源码精读

**目录文件清单**：`opbase_test/` 下共 22 个文件（11 对 `.h/.cc`），实际磁盘内容（`ls` 可验证）与 [README.md:L22-L33](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/README.md#L22-L33) 的目录树一致。

**算子 ↔ 源文件 ↔ 二进制 ↔ HCCL API 对照表**（源文件映射来自 [Makefile:L56-L66](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L56-L66)，API 与特性列来自 [README.md:L272-L286](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/README.md#L272-L286) 的支持算子表）：

| 算子 | 测试源文件（opbase_test/） | 二进制名 | HCCL API | 归约参数 `-o` | Root 参数 `-r` |
|------|---------------------------|----------|----------|:---:|:---:|
| AllGather | hccl_allgather_rootinfo_test.cc | all_gather_test | HcclAllGather | 不生效 | 不生效 |
| AllGatherV | hccl_allgatherv_rootinfo_test.cc | all_gatherv_test | HcclAllGatherV | 不生效 | 不生效 |
| AllReduce | hccl_allreduce_rootinfo_test.cc | all_reduce_test | HcclAllReduce | 生效 | 不生效 |
| AlltoAll | hccl_alltoall_rootinfo_test.cc | alltoall_test | HcclAlltoAll | 不生效 | 不生效 |
| AlltoAllV | hccl_alltoallv_rootinfo_test.cc | alltoallv_test | HcclAlltoAllV | 不生效 | 不生效 |
| AlltoAllVC | hccl_alltoallvc_rootinfo_test.cc | alltoallvc_test | HcclAlltoAllVC | 不生效 | 不生效 |
| Broadcast | hccl_brocast_rootinfo_test.cc | broadcast_test | HcclBroadcast | 不生效 | 生效 |
| Reduce | hccl_reduce_rootinfo_test.cc | reduce_test | HcclReduce | 生效 | 生效 |
| ReduceScatter | hccl_reducescatter_rootinfo_test.cc | reduce_scatter_test | HcclReduceScatter | 生效 | 不生效 |
| ReduceScatterV | hccl_reducescatterv_rootinfo_test.cc | reduce_scatterv_test | HcclReduceScatterV | 生效 | 不生效 |
| Scatter | hccl_scatter_rootinfo_test.cc | scatter_test | HcclScatter | 不生效 | 生效 |

规律很清晰：**只有归约族算子的 `-o`（sum/prod/max/min）有意义，只有「结果落在一张卡上」或「从一张卡发出」的算子（Broadcast/Reduce/Scatter）的 `-r` 有意义**——这与集合通信语义完全对应。

**类名 ↔ 二进制对照**：[README.md:L70-L80](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/README.md#L70-L80) 列出了 11 个算子测试类名（如 `HcclOpBaseAllreduceTest`、`HcclOpBaseBrocastTest`），它们与上表二进制一一对应，每个类在对应 `.cc` 的 `init_opbase_ptr` 中被 new 出（见 4.2.3）。

#### 4.3.4 代码实践

**实践：亲手统计 opbase_test 的算子清单并核对 Makefile**

1. **实践目标**：不依赖 README，仅用 shell 命令从源码目录独立推导出「算子名 / 测试文件 / 二进制名」三元组，并与本讲 4.3.3 的表格互相印证。
2. **操作步骤**：
   1. 在仓库根目录执行 `ls src/hccl_test/opbase_test/*.cc | xargs -n1 basename`，得到 11 个源文件名；
   2. 执行 `grep -E "^[a-z_]+_test: SRC" src/hccl_test/Makefile`，得到 11 行「二进制名 → 源文件」映射；
   3. 执行 `grep -h "class HcclOpBase.*Test" src/hccl_test/opbase_test/*.h`，得到 11 个测试类名；
   4. 把三路结果拼成一张表，再与 [README.md:L272-L286](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/README.md#L272-L286) 的官方表格对照。
3. **需要观察的现象**：三路统计的数量都应该是 11；`broadcast` 的源文件名是 `hccl_brocast_...`（拼写少一个 ad），而二进制名和类名分别是 `broadcast_test`、`HcclOpBaseBrocastTest`（类名也继承了这个拼写）。
4. **预期结果**：得到与 4.3.3 表格一致的清单。此实践纯文件操作，无需昇腾环境，可直接完成。

#### 4.3.5 小练习与答案

**练习 1**：AllReduce 和 Reduce 的测试在参数行为上有什么关键区别？

**参考答案**：两者 `-o`（归约操作）都生效，区别在 `-r`：AllReduce 是「全员各执一份相同结果」，不需要 root；Reduce 是「结果只落在 root rank 上」，所以 `-r` 生效（见 4.3.3 表格，出处 [README.md:L278](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/README.md#L278) 与 [README.md:L283](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/README.md#L283)）。

**练习 2**：AllGatherV 相对 AllGather「V」在哪里？从哪里能看出线索？

**参考答案**：V 表示变长（Variable）——各 rank 提供的数据量可以不同。线索一是 README 目录树里的注释（[README.md:L24](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/README.md#L24) 标注 AllGatherV 算子测试）；二是 4.3.3 表中两者 API 分别为 `HcclAllGather` / `HcclAllGatherV`，对应 HCCL 接口上接收每 rank 独立 count 的变长形态。具体到源码层面的差异（recv 内存布局如何按各 rank 分别计算）将在 u5-l2 精读算子类时展开。

**练习 3**：如果一个新算子 `HcclGather`（gather 到 root）要加入测试，需要新建哪些文件、改哪些既有文件？

**参考答案**：新建 `opbase_test/hccl_gather_rootinfo_test.h/.cc`（继承 `HcclOpBaseTest`，实现 `init_opbase_ptr`/`delete_opbase_ptr` 与算子逻辑）；改 [Makefile:L37](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L37) 的 `LIST` 加 `gather_test` 并加一行 `gather_test: SRC = hccl_gather_rootinfo_test.cc`。common 下的 main 与基类**零改动**——这就是工厂模式 + 继承体系的收益（u6-l4 二次开发实战会完整走一遍这个流程）。

### 4.4 hostfile：多机场景的节点清单

#### 4.4.1 概念说明

仓库里的 [hostfile](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/hostfile) 是一个只有一行注释的模板文件（[hostfile:L1](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/hostfile#L1)：`# 训练节点ip:每节点的进程数`）。它的作用是告诉 `mpirun`：**这次测试要在哪些机器上、每台机器拉起几个测试进程**。

- **单机场景**：不需要 hostfile，`mpirun -n 8 ./bin/all_reduce_test ...` 即可；
- **多机场景**：必须提供 hostfile（MPICH 用 `-f` 指定，Open MPI 用 `-hostfile`），mpirun 会通过 SSH 把测试二进制分发到各节点执行，各节点的进程再共同组成一个 HCCL 通信域。

注意 hostfile 是 **MPI 的配置**，不是 HCCL 的：hccl_test 借助 MPI 完成多机进程编排，HCCL 通信域建立后真正的集合通信数据走昇腾的高速互联（HCCS/网络），与 MPI 无关。MPI 只在启动阶段和「通信域根信息广播」（u5-l2 的 `init_hcclComm`）中参与。

#### 4.4.2 核心流程

多机测试的组织方式：

```text
用户编辑 hostfile（每节点一行：ip:进程数 或 节点名 slots=进程数）
  └─ mpirun -f hostfile -n <NPU总数> ./bin/all_reduce_test <测试参数>
      ├─ 节点A：拉起 8 个 all_reduce_test 进程（rank 0~7）
      ├─ 节点B：拉起 8 个 all_reduce_test 进程（rank 8~15）
      └─ 每个进程：main() → MPI_Init → get_mpi_proc 发现本机进程布局
           → device_init 各自绑定一张 NPU → init_hcclComm（MPI 广播 rootinfo）
           → start_test 遍历数据量执行 AllReduce → 输出带宽/时延
```

其中 `-n` 的值 = 节点数 × 每节点 NPU 数，hostfile 中每节点进程数之和应与之匹配。

#### 4.4.3 源码精读

**hostfile 模板本体**。[hostfile:L1](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/hostfile#L1)：整个文件就是一行注释 `# 训练节点ip:每节点的进程数`，给出 MPICH 格式的填写提示。安装后它随 CMake install 规则落在 `${INSTALL_LIBRARY_DIR}/tools/hccl_test/hostfile`，用户文档 [execution.md:L119-L121](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md#L119-L121) 明确说明「可直接基于该模版进行编辑，也可自定义路径与名称」。

**两种 MPI 实现的格式差异**。[execution.md:L123-L145](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md#L123-L145)：MPICH 场景（仅支持 IPv4）每行格式为 `节点ip:每节点的进程数`，如 `10.10.130.22:8`；Open MPI 场景每行格式为 `节点名 slots=每节点的进程数`，如 `node3 slots=8`。模板文件里的注释采用的是 MPICH 格式的提示。

**多机启动命令示例**。[execution.md:L168](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md#L168)（MPICH）与 [execution.md:L201](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md#L201)（Open MPI，注意多机时需 `-x` 透传 `ASCEND_HOME_PATH`、`LD_LIBRARY_PATH` 等 CANN 环境变量，并常配 `--prefix` 指定 MPI 安装路径）。参数说明文档 [cmdline_options_desc.md:L26-L28](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/cmdline_options_desc.md#L26-L28) 也再次强调：单机无需 hostfile，多机必须配置。

**hostfile 的另一个用途：故障清理**。测试异常退出后可能残留各节点上的测试进程，[faqs.md:L84-L96](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/faqs.md#L84-L96) 给出的清理方式正是复用 hostfile：`mpirun -f hostfile -n 512 pkill -9 -f "all_reduce_test|mpirun"`——借 MPI 的节点清单到每台机器上批量杀进程。这说明 hostfile 是多机运维的「节点账本」，不止服务于启动。

#### 4.4.4 代码实践

**实践：为一个假想的双节点集群编写 hostfile 并组装启动命令**

1. **实践目标**：掌握 hostfile 的两种格式，并能拼出一条完整的多机 AllReduce 测试命令。
2. **操作步骤**：
   1. 假设集群有两台机器：IP 为 `192.168.1.11` 和 `192.168.1.12`，各 8 张 NPU。分别写出 MPICH 与 Open MPI 两种格式的 hostfile 内容（在纸面或临时文件里写，不要改动仓库内的模板文件）；
   2. 参照 [execution.md:L168](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md#L168)，写出 MPICH 场景下 16 卡 fp32 AllReduce 测试的完整命令；
   3. 检查命令中 `-n` 的值（应为 16）与 hostfile 进程数总和（8+8）是否一致；
   4. 如有真实双机环境则实际执行验证；没有则在纸面完成。
3. **需要观察的现象**（有环境时）：mpirun 会在两台机器上各拉起 8 个进程；每个 rank 输出一行测试进度，最终每个数据量档位输出一条耗时与带宽记录。
4. **预期结果**：
   - MPICH 格式：两行 `192.168.1.11:8`、`192.168.1.12:8`；
   - Open MPI 格式：两行 `node1 slots=8`、`node2 slots=8`；
   - 启动命令形如 `mpirun -f hostfile -n 16 ./bin/all_reduce_test -p 8 -b 8K -e 64M -f 2 -d fp32 -o sum`。
   - 真实运行效果**待本地验证**（需要双节点昇腾环境）。

#### 4.4.5 小练习与答案

**练习 1**：hostfile 中写 `192.168.1.11:8`，启动命令写 `-n 20`，会发生什么？

**参考答案**：hostfile 声明的进程总数（8）与 `-n` 请求的进程数（20）不匹配，mpirun 无法把 20 个进程按清单分配到节点上，通常会直接报错退出。正确做法是让 `-n` = 节点数 × 每节点 NPU 数（文档 [cmdline_options_desc.md:L29](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/cmdline_options_desc.md#L29) 的说明）。具体报错文案依 MPI 实现而异，**待本地验证**。

**练习 2**：为什么说「hostfile 是 MPI 的配置，不是 HCCL 的」？

**参考答案**：hostfile 只被 `mpirun` 读取，用于决定在哪些节点拉起多少个测试进程；它不进入 HCCL 的任何接口。HCCL 通信域的建立发生在各进程启动之后（由 `init_hcclComm` 完成，且其根信息同步恰恰借用 MPI 广播），通信数据面走昇腾互联而非 MPI。所以换掉 MPI 实现只需要改 hostfile 格式（`ip:数` vs `slots=`）和 mpirun 参数（`-f` vs `-hostfile`），hccl_test 源码不变。

## 5. 综合实践

**任务：为 hccl_test 写一份《新算子接入指南》的调研笔记**

目标：把本讲三个模块（构建、入口、算子目录）串成一次真实的「假如要加一个新算子」的调研。步骤：

1. **构建侧**：抄录 [Makefile:L37](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L37) 的 `LIST` 与 [Makefile:L56-L66](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L56-L66) 的映射语法，写出新增 `gather_test` 目标需要追加的两行；
2. **入口侧**：阅读 [hccl_test_main.cc:L36-L43](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_main.cc#L36-L43)，确认 main 是否需要任何改动（答案应为：不需要，因为工厂函数 `init_opbase_ptr` 由算子 `.cc` 提供实现），并用一句话解释为什么不需要；
3. **算子侧**：从 4.3.3 表格中任选一个语义最接近 Gather 的算子（如 Scatter 或 Reduce），记录其源文件路径，作为新算子实现时的参照模板；
4. **运维侧**：说明新二进制 `gather_test` 装到用户机器后，单机 8 卡怎么跑、双机 16 卡怎么配 hostfile 跑（参照 4.4.4 的命令模板）。

产出：一页 Markdown 笔记，包含上述四点结论。这份笔记将在 u5-l2（理解基类骨架）和 u6-l4（二次开发实战）中被反复充实。

## 6. 本讲小结

- hccl_test 是基于 HCCL 单算子 API 的 C++ 测试工具，用 MPI 编排多进程/多机，验证 11 种集合通信算子的正确性并测量通信带宽/时延。
- 构建是双入口分工：**Makefile 编译**（产出 `bin/` 下 11 个二进制，需用户现场提供 `ASCEND_DIR` 与 `MPI_HOME`），**CMakeLists 只安装**（把整个目录原样搬进 .run 包的 `tools/` 下，不编译任何代码）。
- 11 个二进制共享同一份 main（`hccl_test_main.cc`），通过「每算子一份 `init_opbase_ptr` 工厂函数 + 链接期决定实现」的多态手法实现「一份入口、多个算子」；main 按「初始化 → 测试 → 分级清理」编排全流程。
- `opbase_test/` 每算子一对 `.h/.cc`，覆盖归约族（AllReduce/Reduce/ReduceScatter(V)）、聚集散射族（AllGather(V)/Scatter）、全交换族（AlltoAll(V/VC)）和广播族（Broadcast）；注意 `brocast` 的历史拼写。
- hostfile 是 MPI 的多机节点清单模板：MPICH 用 `ip:进程数` 格式配 `-f`，Open MPI 用 `节点名 slots=进程数` 格式配 `-hostfile`；单机场景不需要它。

## 7. 下一步学习建议

下一讲 **u5-l2「opbase 测试框架：rootinfo 测试如何组织」** 将向下钻一层，精读 `common/src/` 的三块基石：

1. `hccl_test_common.cc` —— `HcclTest` 基类如何做参数解析、MPI 进程发现、ACL 设备初始化与通信域构建（`init_hcclComm` 的 MPI 广播 + `HcclCommInitRootInfo` 流程）；
2. `hccl_opbase_rootinfo_base.cc` —— 中间基类 `HcclOpBaseTest` 的数据量计算、溢出检测与校验框架；
3. `hccl_allreduce_rootinfo_test.cc` —— 以最常用的 AllReduce 为例看叶节点类如何覆写虚函数。

建议先自行浏览 [src/hccl_test/common/src/hccl_test_common.h](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.h) 中 `HcclTest` 类的公开方法列表，对照本讲 main 的调用顺序建立印象，再进入下一讲。
