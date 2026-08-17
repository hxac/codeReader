# 运行第一个示例：init 示例解析

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立编译并运行 `examples/init` 示例，理解 `-mode`、`-pesize` 等运行参数的含义。
2. 掌握 SHMEM 程序的最小调用骨架：`aclInit` → `aclrtSetDevice` → `aclshmemx_init_attr` → 业务逻辑 → `shmem_finalize` → `aclFinalize`。
3. 读懂 `main.cpp` 中「一份源码、五种模式」的条件编译组织方式，以及 `run.sh` 如何把 shell 参数翻译成编译宏和多进程拉起命令。
4. 会修改示例打印 `aclshmem_my_pe()` 与 `aclshmem_n_pes()`，为后续 Host 侧编程（第 3 单元）打好基础。

## 2. 前置知识

本讲承接 u1-l1（项目总览）、u1-l2（编译安装）、u1-l3（目录结构）。你已经知道：

- **PE（Processing Element）**：SHMEM 中的通信参与者编号，从 0 开始；每个操作系统进程对应一个 PE。
- **对称堆**：各 PE 上按相同规则分配的 device 内存区域，远端地址 = 对端堆基址 + 本地堆内偏移。初始化时用 `local_mem_size` 声明它的大小。
- **`shmem.h` 是唯一总入口**，`include/host/` 下的子头文件由它间接展开。
- 编译产物是 `libshmem.so`，安装到 `install/shmem/lib`，通过 `source install/set_env.sh` 导出环境变量。

本讲新增三个基础概念：

- **ACL 运行时**：昇腾 CANN 的设备管理接口（`libascendcl`）。任何要使用 NPU 设备内存的程序，都必须先 `aclInit` 初始化运行时、再 `aclrtSetDevice` 绑定一张卡。SHMEM 建立在对称堆（device 内存）之上，所以这两步是 SHMEM 初始化的前置条件。
- **Bootstrap（引导建链）**：多个 PE 在使用对称内存之前，需要先互相「认识」——交换地址、端口、堆信息。这个互相认识的阶段叫 bootstrap。SHMEM 提供三种模式：Default（TCP 服务）、MPI（借用 MPI 通信）、UniqueID（一个可以在进程间传递的「接头暗号」）。第 2 单元会深入其内部实现，本讲只需会用。
- **条件编译**：用 `#ifdef 宏名` 让同一份 `.cpp` 在不同编译选项下编出不同行为的程序。init 示例正是用这个技巧把五种初始化模式装进了一个 `main.cpp`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/init/main.cpp](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp) | init 示例主程序，一份源码包含五种初始化模式 |
| [examples/init/CMakeLists.txt](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/CMakeLists.txt) | 把 `RUN_MODE` 数字翻译成 `RUN_WITH_*` 编译宏 |
| [examples/init/run.sh](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh) | 解析 `-mode`/`-pesize` 等参数，完成编译并拉起多个进程 |
| [examples/init/README.md](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/README.md) | 官方运行说明（含跨机运行方法） |
| [include/host/init/shmem_host_init.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/init/shmem_host_init.h) | 初始化/终结/uniqueid 相关 API 声明 |
| [include/host/shmem_host_def.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h) | bootstrap 枚举、`aclshmemx_init_attr_t` 结构体、错误码定义 |
| [include/host/team/shmem_host_team.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/team/shmem_host_team.h) | `aclshmem_my_pe` / `aclshmem_n_pes` 声明（综合实践要用） |
| [scripts/run_examples.sh](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/scripts/run_examples.sh) | 仓库级示例批量运行脚本（注意：当前未收录 init 示例，见 4.4） |

## 4. 核心概念与源码讲解

### 4.1 init 示例的生命周期：从 aclInit 到 shmem_finalize

#### 4.1.1 概念说明

这是你接触的第一个完整 SHMEM 程序。它不做任何数据通信，只验证一件事：**一组进程能否成功完成 SHMEM 初始化并干净地退出**。虽然简单，它的调用顺序就是所有 SHMEM 程序的通用骨架：

```text
aclInit            —— 初始化 CANN ACL 运行时（SHMEM 的地基）
aclrtSetDevice     —— 绑定一张 NPU 卡
aclshmemx_init_attr —— SHMEM 初始化：bootstrap 建链 + 建对称堆（集体操作，所有 PE 都要调用）
（业务逻辑，本示例为空）
shmem_finalize     —— SHMEM 逆序释放资源（集体操作）
aclrtResetDevice   —— 解绑设备
aclFinalize        —— 释放 ACL 运行时
```

两个要点：

1. **顺序不能乱**：`aclshmemx_init_attr` 内部要分配 device 内存做对称堆，所以必须发生在 `aclInit`/`aclrtSetDevice` 之后；释放时则按相反顺序，先 `shmem_finalize` 再 `aclrtResetDevice`/`aclFinalize`。
2. **初始化和终结都是集体操作**：所有 PE 必须都调用 `aclshmemx_init_attr`，它在内部会做控制面同步（等所有 PE 到齐、交换完堆信息才返回）。漏掉任何一个 PE，其余 PE 会一直等待直至超时（默认 120 秒，见 4.3）。

#### 4.1.2 核心流程

以最典型的 `RUN_WITH_UNIQUEID` 分支为例，单个 PE 的执行流程：

```text
启动
 ├─ MPI_Init（仅用于辅助获取 pe 编号和广播 uid）
 ├─ pe = MPI 进程号；pe_size = MPI 进程总数
 ├─ aclInit(nullptr)
 ├─ device_id = pe % g_npu     # g_npu 为本机卡数，8 个 PE 用 8 张卡时 device_id == pe
 ├─ aclrtSetDevice(device_id)
 ├─ 若 pe == 0：aclshmemx_get_uniqueid(&uid)   # 只有 PE0 生成 uid
 ├─ MPI_Bcast 把 uid 广播给所有 PE
 ├─ aclshmemx_set_attr_uniqueid_args(...)       # 把 pe/pe_size/堆大小/uid 填进 attributes
 ├─ aclshmemx_init_attr(ACLSHMEMX_INIT_WITH_UNIQUEID, &attributes)
 ├─ 打印 "shmem init SUCCESS"
 ├─ shmem_finalize()
 ├─ aclrtResetDevice / aclFinalize / MPI_Finalize
 └─ 打印 "[SUCCESS] ... demo run success!"
```

注意「PE0 生成 uid → 广播 → 全体用 uid 初始化」这个三步走：uid 就是一次性「房间号」，所有 PE 拿着同一个 uid 去 bootstrap 服务处集合。

#### 4.1.3 源码精读

头文件包含。`shmem.h` 是总入口，`acl/acl.h` 提供 ACL 运行时接口；只有依赖 MPI 的模式才包含 `mpi.h`：

- [examples/init/main.cpp:L16-L21](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L16-L21) —— 包含 ACL 头文件与 `shmem.h`；当定义了 `RUN_WITH_UNIQUEID`、`RUN_WITH_MPI` 等宏时额外包含 `mpi.h`。

CANN 侧准备。任何 SHMEM 调用之前先初始化 ACL 运行时并绑定设备：

- [examples/init/main.cpp:L37-L39](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L37-L39) —— `aclInit(nullptr)` 后用 `device_id = pe % g_npu` 计算本进程绑哪张卡，再 `aclrtSetDevice(device_id)`。当进程数不超过卡数时 `device_id` 与 `pe` 相同。

声明初始化属性并生成、广播 uniqueid：

- [examples/init/main.cpp:L41-L49](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L41-L49) —— 声明 `aclshmemx_init_attr_t attributes` 和 `aclshmemx_uniqueid_t uid`（用 `ACLSHMEM_UNIQUEID_INITIALIZER` 置零）；`local_mem_size` 设为 1 GiB；仅 PE0 调用 `aclshmemx_get_uniqueid` 生成 uid，随后 `MPI_Bcast` 广播给全组。

填充属性并执行初始化：

- [examples/init/main.cpp:L50-L53](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L50-L53) —— `aclshmemx_set_attr_uniqueid_args` 把 `pe`、`pe_size`、`local_mem_size`、`uid` 填入 `attributes`；随后 `aclshmemx_init_attr(ACLSHMEMX_INIT_WITH_UNIQUEID, &attributes)` 完成集体初始化。成功返回 `ACLSHMEM_SUCCESS`（即 0）。

逆序释放：

- [examples/init/main.cpp:L64-L67](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L64-L67) —— `shmem_finalize()`（这是 `aclshmem_finalize` 的短名别名宏）→ `aclrtResetDevice(pe)` → `aclFinalize()` → `MPI_Finalize()`，与初始化顺序严格相反。

`shmem_finalize` 的多实例语义在头文件注释中有明确说明：

- [include/host/init/shmem_host_init.h:L177-L196](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/init/shmem_host_init.h#L177-L196) —— 单实例模式下终结实例 0；多实例模式下终结当前上下文实例。多实例场景推荐直接用带 `instance_id` 的 `aclshmemx_finalize`（[include/host/init/shmem_host_init.h:L198-L209](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/init/shmem_host_init.h#L198-L209)）。本讲的 init 示例只用单实例，多实例留到 u8-l1。

#### 4.1.4 代码实践

**实践目标**：不运行程序，仅靠源码画出 init 示例的调用顺序图，并标注每一步失败时程序的退出路径。

**操作步骤**：

1. 打开 [examples/init/main.cpp](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp)，定位 `RUN_WITH_UNIQUEID` 分支（L24-L75）。
2. 按出现顺序抄下所有对外 API 调用（MPI 的、ACL 的、SHMEM 的各用不同颜色标记）。
3. 单独用红笔标出两个错误分支：初始化失败（L55-L61）和终结失败（L68-L70），观察它们各打印什么、返回什么。

**需要观察的现象**：你会得到一条「两条岔路的直线」——主干是 8 个左右顺序调用，岔路是两个错误出口；所有错误出口都会先释放已获取的资源再 return 1。

**预期结果**：顺序图为 `MPI_Init → aclInit → aclrtSetDevice → (PE0: get_uniqueid) → MPI_Bcast → set_attr_uniqueid_args → aclshmemx_init_attr → shmem_finalize → aclrtResetDevice → aclFinalize → MPI_Finalize`。若你的顺序图与此一致，说明你已经掌握本节内容。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `aclshmemx_init_attr` 必须在 `aclrtSetDevice` 之后调用？

**答案**：`aclshmemx_init_attr` 要为对称堆分配 device 内存并建立跨 PE 映射，这些操作都作用于「当前设备」；设备尚未绑定（或 ACL 运行时未初始化）时无从分配。反过来，释放时对称堆必须先于设备解绑销毁，所以 `shmem_finalize` 在 `aclrtResetDevice` 之前。

**练习 2**：如果 4 个 PE 中有一个进程忘记调用 `aclshmemx_init_attr`，会发生什么？

**答案**：`aclshmemx_init_attr` 是集体操作，内部有控制面同步（bootstrap 阶段等所有 PE 交换信息）。缺一个 PE 时其余 3 个会阻塞等待，直到超时（可选属性里三个 timeout 字段默认 120 秒，见 4.3.3）后返回错误。这也是排查「示例卡住不动」问题的第一怀疑点。

**练习 3**：观察 [examples/init/main.cpp:L57](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L57)：错误分支里 `aclrtResetDevice(pe)` 传入的是 `pe` 而不是 `device_id`。什么场景下两者不相等？可能有什么影响？

**答案**：当进程数超过单机卡数时（例如 2 机 8 卡跑 16 PE，`device_id = pe % 8`），`pe != device_id`。此时 `aclrtResetDevice(pe)` 复位的是「别的设备号」而非本进程绑定的设备，本进程实际持有的设备未被正确复位（对未绑定的设备调用通常只是返回错误码，不会崩溃，具体行为待本地验证）。这是训练「逐行读代码」眼力的一个好例子——示例代码也值得带着怀疑去读。

### 4.2 一份 main.cpp 五种模式：条件编译分派

#### 4.2.1 概念说明

`examples/init/` 只有一个 `main.cpp`，却能以五种（算上压测变体是六种）方式运行。它没有用命令行参数区分模式，而是用**编译期宏**：编译时通过 `-DRUN_WITH_XXX` 注入一个宏，`#ifdef` 只保留对应分支的 `run_main` 函数，其余分支整体被预处理器删掉。

这种做法的好处：各模式的代码互相隔离、零运行时开销；代价是换模式必须重新编译——这正是 `run.sh` 每次都重新跑 cmake + make 的原因。

#### 4.2.2 核心流程

```text
run.sh -mode mpi
  └─ MODE_ID=2
      └─ cmake -DRUN_MODE=2 ..
          └─ CMakeLists: COMPILE_DEF = RUN_WITH_MPI
              └─ target_compile_definitions 注入宏
                  └─ main.cpp 中 #ifdef RUN_WITH_MPI 的 run_main 生效
                      └─ main() 调用该 run_main
```

#### 4.2.3 源码精读

`main()` 的分派逻辑——所有模式都提供同名函数 `run_main`，`main` 只做一次选择：

- [examples/init/main.cpp:L385-L395](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L385-L395) —— 按宏定义把参数转给对应的 `run_main`；一个宏都没定义时打印错误并返回 1，提示必须五选一。

CMake 把数字翻译成宏：

- [examples/init/CMakeLists.txt:L73-L80](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/CMakeLists.txt#L73-L80) —— 强制要求 `RUN_MODE`；`RUN_MODE=1` 映射 `RUN_WITH_DEFAULT` 且不需要 MPI。
- [examples/init/CMakeLists.txt:L81-L96](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/CMakeLists.txt#L81-L96) —— `RUN_MODE=2`/`3` 分别映射 `RUN_WITH_MPI`/`RUN_WITH_UNIQUEID`，都要 `find_package(MPI REQUIRED)`。
- [examples/init/CMakeLists.txt:L97-L120](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/CMakeLists.txt#L97-L120) —— `RUN_MODE=4/5/6` 映射多实例、uid+default 组合；其中 `RUN_MODE=6` 额外定义 `STRESS_LOOP_COUNT=20`，让多实例创建/销毁循环 20 次做压测。
- [examples/init/CMakeLists.txt:L125-L137](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/CMakeLists.txt#L125-L137) —— `add_executable(init_examples main.cpp)`；用 `target_compile_definitions` 注入选中的宏；需要 MPI 时链接 `MPI::MPI_CXX shmem`，否则只链 `shmem`。

五个分支与五种模式的对应关系（速查表）：

| 宏 | 分支位置 | bootstrap 模式 | 是否需要 MPI | 典型用途 |
| --- | --- | --- | --- | --- |
| `RUN_WITH_DEFAULT` | [L312-L383](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L312-L383) | `ACLSHMEMX_INIT_WITH_DEFAULT` | 否 | 无 MPI 环境、脚本手工拉进程 |
| `RUN_WITH_MPI` | [L265-L310](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L265-L310) | `ACLSHMEMX_INIT_WITH_MPI` | 是 | 已有 MPI 集群 |
| `RUN_WITH_UNIQUEID` | [L23-L76](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L23-L76) | `ACLSHMEMX_INIT_WITH_UNIQUEID` | 是（用来广播 uid） | 用 uid 建链 |
| `RUN_UNIQUEID_WITH_DEFAULT` | [L78-L131](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L78-L131) | `ACLSHMEMX_INIT_WITH_DEFAULT` | 是 | uid 生成走 default 通道的组合验证 |
| `RUN_WITH_UNIQUEID_MULTI_INSTANCE` | [L133-L263](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L133-L263) | `ACLSHMEMX_INIT_WITH_UNIQUEID` | 是 | 单进程多实例（u8-l1 再深入） |

#### 4.2.4 代码实践

**实践目标**：验证「换模式必须重编译」，并体验条件编译的开关效果。

**操作步骤**：

1. 进入 `examples/init/`，执行只编译不运行的命令：
   ```bash
   bash run.sh -build -mode default
   ```
   `-build` 使脚本编译后直接退出（见 [examples/init/run.sh:L94-L97](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh#L94-L97) 与 [L168-L171](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh#L168-L171)）。
2. 再执行 `bash run.sh -build -mode uid`，观察输出中 cmake 的 `RUN_MODE` 数值变化（3 vs 1）。
3.（可选，无 MPI 环境时的替代观察）直接查看编译数据库或手动执行：
   ```bash
   cd build && cmake -DRUN_MODE=1 .. && make init_examples -j$(nproc)
   ```
   然后运行 `./build/bin/init_examples`（不带参数），观察它打印的报错信息。

**需要观察的现象**：`-mode default` 与 `-mode uid` 触发的 `RUN_MODE` 分别是 1 和 3；模式 2 及以上需要系统安装 MPI，否则 cmake 阶段报 `find_package(MPI REQUIRED)` 失败。

**预期结果**：`-mode default` 在只有 CANN 环境、没有 MPI 的机器上也能编译通过（`NEED_MPI=OFF`）；其余模式编译需要 MPI。步骤 3 中直接运行不带宏产物时，程序打印 `Error: Please define one of RUN_WITH_UNIQUEID/...`（不过 `run.sh` 编译产物总是带了某个宏，只有手工用 `g++`/`cmake` 不定义宏编译才会看到这条，本环境未复现，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `run.sh` 每次运行都要 `rm -rf build/*` 重新 cmake，而不是复用上次的构建？

**答案**：因为模式选择是编译期宏。如果缓存了上次 `-DRUN_MODE=1` 的构建产物，这次想以 uid 模式运行就会拿到旧的二进制。清空重建（[examples/init/run.sh:L146-L150](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh#L146-L150)）保证二进制与当前模式严格一致。这也意味着你修改 `main.cpp` 后直接重跑 `run.sh` 即可生效，不需要手动 make。

**练习 2**：`RUN_MODE=6`（uid_multi_stress）相对 `RUN_MODE=4`（uid_multi）多了什么？

**答案**：多定义了 `STRESS_LOOP_COUNT=20`（[examples/init/CMakeLists.txt:L113-L120](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/CMakeLists.txt#L113-L120)）。`main.cpp` 中用它控制多实例创建/销毁循环 20 次（[examples/init/main.cpp:L222-L249](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L222-L249)），并统计失败次数；同时该宏会让每轮的 SUCCESS 日志被抑制，避免刷屏。

### 4.3 初始化 API 三件套与 aclshmemx_init_attr_t

#### 4.3.1 概念说明

`aclshmemx_init_attr` 是 SHMEM 初始化的统一入口，它吃两个参数：一个**bootstrap 模式标志**和一个**属性结构体指针**。属性结构体 `aclshmemx_init_attr_t` 描述「我是谁（my_pe）、全组多少人（n_pes）、要建多大的对称堆（local_mem_size）、去哪里接头（ip_port / uid）」。

围绕它有三件套 API：

- `aclshmemx_get_uniqueid`：生成一个 uid（只需 PE0 调用）。
- `aclshmemx_set_attr_uniqueid_args`：官方推荐的属性填充器，避免手写结构体出错。
- `aclshmemx_init_attr`：真正执行初始化。

另外还有一个查询接口 `aclshmemx_init_status`，可以在任意时刻询问「现在初始化到哪一步了」。

#### 4.3.2 核心流程

属性结构体的字段与三种模式的取值来源：

```text
aclshmemx_init_attr_t
 ├─ my_pe            ← MPI 进程号 / 脚本计算值 f_pe + device_id
 ├─ n_pes            ← MPI 进程总数 / 脚本 -pesize
 ├─ ip_port[]        ← default 模式：bootstrap 服务器地址 "tcp://ip:port"
 ├─ local_mem_size   ← 示例固定 1 GiB
 ├─ option_attr      ← 版本号 + 引擎类型 + 三个超时（默认 120s）
 ├─ comm_args        ← uid 模式：指向 aclshmemx_uniqueid_t
 └─ instance_id      ← 多实例编号，单实例保持默认 0
```

#### 4.3.3 源码精读

bootstrap 模式枚举——注意这是位标志（bit flag），将来可用于组合：

- [include/host/shmem_host_def.h:L106-L113](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L106-L113) —— `ACLSHMEMX_INIT_WITH_DEFAULT = 1U << 0`（优先用 ip_port，其次 uid，都没有则报错）、`ACLSHMEMX_INIT_WITH_MPI = 1U << 1`、`ACLSHMEMX_INIT_WITH_UNIQUEID = 1U << 3`。

属性结构体全貌：

- [include/host/shmem_host_def.h:L181-L195](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L181-L195) —— `aclshmemx_init_attr_t` 定义。字段含义见其上方注释（[L165-L180](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L165-L180)）：`my_pe`/`n_pes` 标识身份；`ip_port` 长度为 `ACLSHMEM_MAX_IP_PORT_LEN`（64，见 [L72](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L72)）；`local_mem_size` 是对称堆可分配容量；`comm_args` 存 bootstrap 阶段所需参数（uid 模式下指向 uid）。

可选属性与默认超时：

- [include/host/shmem_host_def.h:L155-L162](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L155-L162) —— `aclshmem_init_optional_attr_t`：版本号、`data_op_engine_type`（默认 `ACLSHMEM_DATA_OP_MTE`，承接 u1-l1 的引擎概念）、三个超时字段。
- [include/host/shmem_host_def.h:L32](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L32) —— `DEFAULT_TIMEOUT = 120`（秒），即 4.1 练习 2 中「缺一个 PE 会等 120 秒」的出处。

三件套 API 声明：

- [include/host/init/shmem_host_init.h:L100-L101](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/init/shmem_host_init.h#L100-L101) —— `aclshmemx_get_uniqueid(uid*)`：生成 uid。
- [include/host/init/shmem_host_init.h:L116-L118](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/init/shmem_host_init.h#L116-L118) —— `aclshmemx_set_attr_uniqueid_args(my_pe, n_pes, local_mem_size, uid*, attr*)`：把 uid 模式所需字段填进 attr，注释明确建议用它构造属性而非手工赋值。
- [include/host/init/shmem_host_init.h:L137-L147](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/init/shmem_host_init.h#L137-L147) —— `aclshmemx_init_attr(bootstrap_flags, attributes*)`：统一初始化入口。
- [include/host/init/shmem_host_init.h:L85-L92](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/init/shmem_host_init.h#L85-L92) —— `aclshmemx_init_status()`：查询初始化状态（未初始化 / 堆已建 / 已完成）。

手写属性的两个真实例子——MPI 模式用聚合初始化一步到位，default 模式用辅助函数逐字段填：

- [examples/init/main.cpp:L283-L287](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L283-L287) —— MPI 模式：`aclshmemx_init_attr_t attributes = {pe, pe_size, "", local_mem_size, {0, ACLSHMEM_DATA_OP_MTE, 120, 120, 120}};` 依次对应 `my_pe, n_pes, ip_port, local_mem_size, option_attr`，随后以 `ACLSHMEMX_INIT_WITH_MPI` 初始化。
- [examples/init/main.cpp:L313-L336](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L313-L336) —— default 模式的 `test_set_attr`：逐字段填 `my_pe`/`n_pes`/`ip_port`/`local_mem_size`/`option_attr`，并把 `comm_args` 指向 uid 结构（default 模式下作为可选回退）。其中 `attr_version = (1 << 16) + sizeof(结构体)` 遵循「高 16 位主版本 + 低 16 位结构大小」的编码约定，结构体变化时版本号自动变化，可用于兼容性校验。

#### 4.3.4 代码实践

**实践目标**：把 4.3.3 中 MPI 分支的聚合初始化逐值对应到结构体字段，检验你对属性结构的理解。

**操作步骤**：

1. 对照 [include/host/shmem_host_def.h:L181-L195](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L181-L195) 的字段顺序，为 `{pe, pe_size, "", local_mem_size, {0, ACLSHMEM_DATA_OP_MTE, 120, 120, 120}}` 中每个值写出目标字段名。
2. 注意 `option_attr` 内层聚合 `{0, ACLSHMEM_DATA_OP_MTE, 120, 120, 120}` 只有 5 个值，而 `aclshmem_init_optional_attr_t` 有 6 个字段——想一下第 6 个字段（`sockFd`）会是什么值。
3. 对比 [examples/init/main.cpp:L326-L332](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L326-L332) 中 `test_set_attr` 给 `version` 填的 `(1 << 16) + sizeof(...)` 与 MPI 分支填的 `0`，思考版本号填 0 是否影响运行。

**需要观察的现象**：纯纸面推导，无运行现象。

**预期结果**：`my_pe←pe`、`n_pes←pe_size`、`ip_port←""`、`local_mem_size←1GiB`、`option_attr←{version=0, engine=MTE, 120, 120, 120}`；内层第 6 个字段 `sockFd` 未在聚合中给出，按 C++ 聚合初始化规则置为 0。版本号填 0 时库侧通常不做严格校验（示例能跑通即为佐证），但头文件注释推荐用 `aclshmemx_set_attr_uniqueid_args` 构造属性以避免此类不一致——具体校验行为可在 u2-l2 的初始化源码走读中确认。

#### 4.3.5 小练习与答案

**练习 1**：default 模式下 `ip_port` 填了 `tcp://127.0.0.1:8666`，这个地址是给谁用的？

**答案**：给 bootstrap 控制面用。default 模式是「PE0 起一个 TCP 服务、其他 PE 连上来」的星型结构，`ip_port` 就是那个服务的地址（本机回环 + 端口 8666）。各 PE 靠它完成建链和信息交换，之后数据面走 NPU 通信引擎，不再经过这条 TCP 通道。细节在 u2-l3 展开。

**练习 2**：`local_mem_size` 设 1 GiB 意味着什么？设成 0 会怎样？

**答案**：它是本 PE 对称堆的用户可分配容量，后续 `aclshmem_malloc` 都从这 1 GiB 里切分；所有 PE 应设置相同的值以保证堆布局一致。设成 0 时常规初始化路径没有可分配容量（对 `aclshmemx_init_attr` 而言堆为空），只有走 `aclshmemx_init_attr_with_buffers`（用户自带 buffer 前缀，u8-l3）时才允许 0——见 [include/host/shmem_host_def.h:L173-L176](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L173-L176) 注释。具体报错行为待本地验证。

**练习 3**：`comm_args` 字段在三种模式下分别装什么？

**答案**：default 模式下示例把一个（零值）uid 指针放进去作为可选回退（default 模式优先 ip_port，其次才看 comm_args 里的 uid，见枚举注释）；uniqueid 模式下指向 `aclshmemx_get_uniqueid` 生成的 uid；MPI 模式下不使用（置空即可）。

### 4.4 run.sh：参数解析与多进程拉起

#### 4.4.1 概念说明

SHMEM 程序是**多进程**程序：每个 PE 是一个独立进程。谁把这些进程拉起来？三种模式两套方案：

- **default 模式**：`run.sh` 用 bash 的 `for` 循环 + `&` 后台符号逐个启动进程，再 `wait` 等它们全部退出。不依赖任何外部工具。
- **mpi/uid 系列**：直接用 `mpirun` 拉起，PE 编号由 MPI 分配。跨机时配合 hostfile。

`run.sh` 同时还兼任「编译器」：每次运行先清空 `build/`、按模式跑 cmake 和 make（承接 4.2 的条件编译），然后才拉进程。

#### 4.4.2 核心流程

```text
run.sh
 ├─ 1. 解析参数：-mode/-pesize/-fpe/-ipport/-gnpus/-fnpu/-build
 ├─ 2. mode 字符串 → MODE_ID 数字（default=1 ... uid_multi_stress=6）
 ├─ 3. 按模式导出环境变量（SHMEM_UID_SESSION_ID / SHMEM_UID_SOCK_IFNAME）
 ├─ 4. 清空 build/ → cmake -DRUN_MODE=$MODE_ID → make init_examples
 ├─ 5.（-build 则到此退出）
 └─ 6. 拉起进程：
      ├─ mpi/uid 系列 → mpirun -np $NUM_PROCESSES ./build/bin/init_examples $NUM_PROCESSES
      └─ default      → for idx in 0..GNPU_NUM-1:
                          ./build/bin/init_examples $idx $NUM_PROCESSES $IPPORT $GNPU_NUM $FIRST_PE $FIRST_NPU &
                         wait 全部子进程
```

default 模式下 PE 编号的推导规则（跨机的关键）：`pe = f_pe + device_id`，其中 `device_id` 是循环变量 `idx`，`f_pe` 来自 `-fpe`。双机各 2 进程时：机器 A `-fpe 0` 得到 pe 0、1；机器 B `-fpe 2` 得到 pe 2、3。

#### 4.4.3 源码精读

默认参数与 `-pesize` 的隐含逻辑：

- [examples/init/run.sh:L13-L20](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh#L13-L20) —— 默认 `MODE=default`、`NUM_PROCESSES=2`、`GNPU_NUM=8`、`IPPORT=tcp://127.0.0.1:8666`。
- [examples/init/run.sh:L28-L39](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh#L28-L39) —— `-pesize` 必须是正整数；**若 `GNPU_NUM > NUM_PROCESSES` 则把 `GNPU_NUM` 压到 `NUM_PROCESSES`**。这一步很关键：default 模式的进程循环次数是 `GNPU_NUM`（见下方拉起代码），所以 `-pesize` 实际是通过压缩 `GNPU_NUM` 来决定进程数的。

模式字符串到 `RUN_MODE` 数字的映射：

- [examples/init/run.sh:L106-L130](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh#L106-L130) —— `default→1`、`mpi→2`、`uid→3`、`uid_multi→4`、`uid_default→5`、`uid_multi_stress→6`，与 4.2 的 CMakeLists 一一对应；非法模式直接报错退出。

环境变量准备：

- [examples/init/run.sh:L135-L144](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh#L135-L144) —— mpi/uid/default/uid_default 模式导出 `SHMEM_UID_SESSION_ID`（默认 `127.0.0.1:8666`，传 `-ipport` 时更新）；uid_multi 系列则要求先手工 `export SHMEM_UID_SOCK_IFNAME=eth0:inet4` 指定网卡，否则打印警告。

编译阶段：

- [examples/init/run.sh:L146-L158](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh#L146-L158) —— 清空并重建 `build/` 目录，`cmake -DRUN_MODE="${MODE_ID}" ..`。
- [examples/init/run.sh:L160-L165](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh#L160-L165) —— `make -j$(nproc) init_examples` 编译出 `build/bin/init_examples`。

两套拉起方式：

- [examples/init/run.sh:L176-L189](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh#L176-L189) —— mpi/uid 系列：有 `hostfile` 就 `mpirun -f hostfile`（跨机），否则 `mpirun -np $NUM_PROCESSES`（单机），并向程序传入本机卡数参数。
- [examples/init/run.sh:L190-L209](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh#L190-L209) —— default 模式：`for` 循环 `GNPU_NUM` 次后台启动，每个进程传 6 个参数 `idx / NUM_PROCESSES / IPPORT / GNPU_NUM / FIRST_PE / FIRST_NPU`，正对应 `main.cpp` default 分支的 `argv[1..6]` 解析（[examples/init/main.cpp:L344-L350](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L344-L350)）；最后 `wait` 逐个收割退出码，任何一个非 0 都判定整轮失败。

一个重要的诚实提醒：任务描述中提到的仓库级脚本 [scripts/run_examples.sh](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/scripts/run_examples.sh) 目前**没有** init 示例的运行入口——它的 `case` 分发表（[L126-L155](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/scripts/run_examples.sh#L126-L155)）只覆盖 allgather、kv_shuffle、rdma_demo 等示例。所以本讲的运行实践以 `examples/init/run.sh` 为准。

#### 4.4.4 代码实践

**实践目标**：在真实机器上以不同 `-pesize` 运行 init 示例；无 NPU 时完成一次「脚本推演」，理解参数如何流动。

**操作步骤（有 CANN + NPU 环境）**：

1. 先在仓库根目录完成编译安装（承接 u1-l2）：
   ```bash
   bash scripts/build.sh
   source install/set_env.sh
   ```
2. 进入示例目录并分别以 2、4 个 PE 运行 default 模式：
   ```bash
   cd examples/init
   bash run.sh -mode default -pesize 2
   bash run.sh -mode default -pesize 4
   ```
3. 再尝试 MPI 模式（需已安装 MPI，见 [examples/init/README.md:L7-L14](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/README.md#L7-L14)）：
   ```bash
   bash run.sh -mode mpi -pesize 2
   ```

**操作步骤（无 NPU 环境，脚本推演）**：

1. 读 [examples/init/run.sh:L28-L39](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh#L28-L39) 与 [L190-L196](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh#L190-L196)，回答：执行 `bash run.sh`（不带任何参数）会启动几个进程？每个进程拿到的 `pe_size` 是多少？
2. 对照 [examples/init/README.md:L40](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/README.md#L40)「不指定时默认 mode=default、pesize=2」的说法，思考 README 的前提假设是什么。

**需要观察的现象**：有 NPU 时，2 PE 运行应输出两条 `pe N: shmem init SUCCESS` 和两条 `[SUCCESS] pe N: demo run success!`（N 为 0 和 1）；4 PE 时是四条，PE 编号 0-3。进程输出顺序可能交错，属正常（多进程并发打印）。

**预期结果（推演）**：不带参数时 `NUM_PROCESSES=2` 但 `GNPU_NUM` 保持默认 8，default 分支的循环会启动 **8 个**进程、每个被告知 `pe_size=2`——与 README「默认 pesize=2」的表述并不一致。因此**建议总是显式传 `-pesize`**（README 的示例命令也都显式传递）。该推演基于脚本源码逐行分析，实际运行表现待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：双机场景，机器 B 执行 `bash run.sh -mode default -pesize 4 -fpe 2 -gnpus 2 -ipport 192.168.1.100:8666`，机器 B 上两个进程的 PE 编号分别是多少？`-ipport` 指向谁的地址？

**答案**：PE 编号 = `f_pe + idx` = 2 + 0 和 2 + 1，即 pe 2 和 pe 3；`-ipport` 指向机器 A（pe0 所在机器）的地址，因为 default 模式的 bootstrap 服务由 PE0 一侧提供，所有其他 PE 都连过去。注意脚本会自动拼 `tcp://` 前缀（[examples/init/run.sh:L53-L67](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/run.sh#L53-L67)）。

**练习 2**：default 模式下，`run.sh` 传给程序的 6 个参数中，`g_npu`（第 4 个）和 `f_npu`（第 6 个）在 [examples/init/main.cpp](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp) 的 default 分支里实际被使用了吗？

**答案**：没有。default 分支在 [L344-L350](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L344-L350) 读入 `g_npu` 和 `f_npu` 后，计算 `pe = f_pe + device_id` 只用了 `f_pe` 和 `device_id`（idx），`g_npu`/`f_npu` 在该分支中是「读了未用」的占位参数（`g_npu` 在 uniqueid/mpi 分支中用于 `device_id = pe % g_npu`，`f_npu` 在所有分支都未参与计算）。这属于历史演化的痕迹，也是读示例代码时应保持的批判性眼光。

**练习 3**：`-ipport` 的端口冲突了会怎样？如何换端口？

**答案**：default 模式 bootstrap 依赖该 TCP 端口（默认 8666）。端口被占用时 PE0 侧服务建立失败，初始化返回 `ACLSHMEM_BOOTSTRAP_ERROR`（错误码 -6，见 [include/host/shmem_host_def.h:L93](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L93)）或超时。换端口用 `-ipport tcp://ip:新端口`——注意直接传 `ip:port` 即可，脚本会补前缀；同时所有机器都要用同一个 `-ipport` 值。具体错误码行为待本地验证。

## 5. 综合实践

**任务**：在运行示例的基础上修改 `main.cpp`，让每个 PE 打印自己的 `aclshmem_my_pe()` 与 `aclshmem_n_pes()`，验证「库视角的身份」与「进程视角的身份」一致。

**步骤**：

1. **确认 API**。这两个接口声明在 [include/host/team/shmem_host_team.h:L74-L90](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/team/shmem_host_team.h#L74-L90)：`aclshmem_my_pe()` 返回 0 到 npes-1 的本 PE 编号，`aclshmem_n_pes()` 返回总 PE 数。它们在初始化成功之后才可调用。

2. **修改代码**（示例代码，非项目原有内容）。以 default 模式分支为例，在初始化成功打印之后、`shmem_finalize()` 之前插入，即 [examples/init/main.cpp:L370](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L370) 与 [L372](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L372) 之间：

   ```cpp
   // 示例代码：打印库视角的 PE 身份
   std::cout << "pe " << pe << ": shmem_my_pe=" << aclshmem_my_pe()
             << ", shmem_n_pes=" << aclshmem_n_pes() << std::endl;
   ```

   如果你习惯用 uid 或 mpi 模式，把同样的两行插到对应分支的 `shmem init SUCCESS` 打印之后即可（各分支都有相同结构的打印点，如 [L62](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L62)、[L296](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp#L296)）。

3. **重新运行**（`run.sh` 每次都会重新编译，改动自动生效）：
   ```bash
   bash run.sh -mode default -pesize 2
   bash run.sh -mode default -pesize 4
   ```

4. **观察并记录**：
   - `aclshmem_my_pe()` 的值是否与程序自己算出的 `pe` 一致？
   - `-pesize 2` 与 `-pesize 4` 时 `aclshmem_n_pes()` 分别是多少？
   - 试着把打印点挪到 `aclshmemx_init_attr` 之前（初始化完成前）再编译运行，观察会发生什么。

**预期结果**：`aclshmem_my_pe()` 与脚本/进程推算的 `pe` 完全一致；`aclshmem_n_pes()` 分别为 2 和 4；若在初始化前调用，预期返回无效值或触发 `ACLSHMEM_NOT_INITED`（-5）相关错误——库明确规定这些查询要在初始化后使用，具体表现待本地验证。

**无 NPU 环境的替代方案**：完成代码修改后只执行 `bash run.sh -build -mode default` 验证编译通过；再手工从 `run.sh` 的拉起命令出发，写出「2 PE 时两个进程各自的 argv 与打印内容」的推演表。

## 6. 本讲小结

- SHMEM 程序的通用骨架是 `aclInit → aclrtSetDevice → aclshmemx_init_attr → 业务 → shmem_finalize → aclrtResetDevice → aclFinalize`，初始化与终结都是**集体操作**，缺一个 PE 其余都会等至超时（默认 120 秒）。
- `aclshmemx_init_attr(bootstrap_flags, attributes)` 是统一初始化入口；`aclshmemx_init_attr_t` 携带 `my_pe`/`n_pes`/`ip_port`/`local_mem_size`/`option_attr`/`comm_args`，uid 模式推荐用 `aclshmemx_set_attr_uniqueid_args` 填充。
- init 示例用条件编译把 Default/MPI/UniqueID/uid+default/多实例五种模式装进一份 `main.cpp`；`run.sh` 负责把 `-mode` 翻译成 `cmake -DRUN_MODE=N` 再编译，因此**换模式必重编译**。
- default 模式由脚本用 `for` 循环 + `&` 拉起 `GNPU_NUM` 个进程，PE 编号 = `f_pe + idx`（跨机用 `-fpe` 偏移）；mpi/uid 系列用 `mpirun` 拉起。
- `-pesize` 在 default 模式下通过把 `GNPU_NUM` 压到进程数来生效；建议总是显式传 `-pesize`。
- `aclshmem_my_pe()` / `aclshmem_n_pes()` 是最常用的身份查询接口，只能在初始化成功后调用。

## 7. 下一步学习建议

本讲你只「会用」了初始化接口，下一讲（u2-l1「初始化 API 与三种 Bootstrap 模式」）将系统对比三种 bootstrap 模式的适用场景与 uniqueid 的生成广播细节；u2-l2 会带你进入 `src/host/init/shmem_init.cpp`，看 `aclshmemx_init_attr` 内部到底做了什么（bootstrap 建链 → 建对称堆 → 子模块就绪三阶段）。在继续之前，建议你：

1. 把综合实践做扎实——`my_pe`/`n_pes` 会贯穿后续所有示例。
2. 重读 [examples/init/README.md](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/README.md) 的跨机运行一节，对照 4.4 的参数推演自己画一张双机 4 PE 的部署图。
3. 有余力的读者可以预习 [include/host/shmem_host_def.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h) 中的错误码枚举，它是之后排查初始化失败的主要工具。
