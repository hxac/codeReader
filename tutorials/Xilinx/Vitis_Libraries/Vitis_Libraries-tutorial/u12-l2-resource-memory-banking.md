# 资源/时序：URAM、HBM/DDR 分区与报告

## 1. 本讲目标

本讲是专家层「性能与数据流架构」的第二讲。上一讲（u12-l1）把 DATAFLOW、SSR、datawidth、II 四个旋钮统一到了一张吞吐公式里；本讲把视线从「计算吞吐」转向「**存储与面积**」，回答三个工程问题：

1. 片上存储器（URAM/BRAM）到底该用哪一种、用多少？URAM 阵列如何做到「每个时钟都能更新」而不被迭代间依赖拖垮 II？
2. 板上多片 DDR/HBM 怎么分区（banking）才能把聚合带宽拉满？这跟主机端的 `group_id` 有什么关系？
3. 综合/实现报告里的资源、II、时序数字在哪里看，如何据此定位瓶颈？

学完后你应当能：理解 `UramArray` 前递缓存与 `cache` 只读行缓存的原理与适用场景；掌握 DDR/HBM bank 分区对带宽的影响；学会从实现报告里定位 II/时序/资源瓶颈，并能看懂 `system.cfg` 里 `sp=` 一行如何决定一个缓冲挂在哪片 DDR 上。

## 2. 前置知识

本讲假设你已经掌握以下内容（来自依赖讲义）：

- **HLS pragma 与 II**（u3-l2、u12-l1）：知道 `#pragma HLS pipeline II=1`、`bind_storage`、`array_partition` 的作用，知道 II（启动间隔）决定吞吐。
- **HLS 五档大写 TARGET 与报告**（u2-l3）：知道 csynth 产出 II/latency/资源报告，vivado_impl 产出时序报告，报告文件位于 `test.prj` 下。
- **buffer object、group_id 与存储端口**（u4-l3）：知道 `xrt::bo` 用 `kernel.group_id(arg)` 挂到内核参数对应的 DDR/HBM bank，而这个组号由 `system.cfg` 的 `sp=` 在链接期决定。

几个本讲会用到的术语先澄清：

- **URAM**：UltraRAM，Xilinx UltraScale+ 器件上的高密度片上存储基本块，单块 4096 深 × 72 位宽，双端口（RAM_2P），密度远高于 BRAM 但端口少、灵活性低。
- **BRAM**：Block RAM，灵活、多端口、可配置宽度，但每块容量小（典型 36Kb），适合小而快的查找表/缓存。
- **LUTRAM**：用查找表（LUT）做的分布式 RAM，容量最小、最快，适合很浅的有效位图。
- **RAW 依赖**（Read-After-Write）：同一存储地址先写后读，HLS 默认会保守地认为相邻迭代之间可能存在这种依赖，从而把 II 顶高。
- **banking（存储分区）**：把不同存储端口绑定到不同的物理 DDR/HBM 通道，使它们能并行访问，聚合带宽随均衡使用的 bank 数近似线性增长。

## 3. 本讲源码地图

本讲聚焦 `utils` 库的片上存储原语与 `dsp` 库的连接配置，涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [utils/L1/include/xf_utils_hw/uram_array.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/uram_array.hpp) | `UramArray` 类：带前递寄存器的 URAM 阵列，支持每周期读写。 |
| [utils/L1/include/xf_utils_hw/cache.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/cache.hpp) | `cache` 类：把 DDR/HBM 的只读数据行缓存到片上 URAM/BRAM。 |
| [utils/L1/tests/uram_array/dut.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/uram_array/dut.cpp) | UramArray 的被测内核，演示 `#pragma HLS DEPENDENCE ... inter false` 的必备用法。 |
| [utils/L1/tests/cache_ro_1DDR_with_e/cache.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/cache_ro_1DDR_with_e/cache.cpp) | 单 DDR 只读缓存用例的顶层（BRAM、单 m_axi 端口）。 |
| [utils/L1/tests/cache_ro_2DDR_with_e/cache.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/cache_ro_2DDR_with_e/cache.cpp) | 双 DDR 只读缓存用例的顶层（URAM、双 m_axi 端口）。 |
| [dsp/L2/examples/vss_fft_ifft_1d/system.cfg](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg) | AIE 系统的连接配置，含 `sp=` 存储端口绑定与 Vivado 报告开关。 |

## 4. 核心概念与源码讲解

### 4.1 UramArray：可每周期更新的 URAM 阵列

#### 4.1.1 概念说明

很多算法需要在片上维护一张大表，并在一个 `for` 循环里**每个时钟都读写它**——比如查找表、状态机记忆、数据依赖的中间结果。URAM 因为密度高（单块 4096×72）是大表的首选，但它有两个工程难点：

1. URAM 是双端口（RAM_2P），一周期只能一读或一写一个地址，写后并不能「立刻」在同一周期读出新值，存在真实的读延迟。
2. 如果在同一个 `II=1` 的循环里先写 `blocks[i]`、紧接着下一迭代又读 `blocks[i-1]`，HLS 会保守地认定存在**迭代间 RAW 依赖**，于是把 II 顶到 ≥ 写延迟，吞吐直接塌掉。

`UramArray` 解决的就是「如何在 URAM 上做到 II=1 的每周期读写」。它的核心思想是用一小撮**前递寄存器（forwarding regs）**记录最近若干次写入的地址与值；读命中前递寄存器时跳过 URAM 读取，从而打断 RAW 依赖。

#### 4.1.2 核心流程

`UramArray` 的读写流程可以这样描述（伪代码）：

```
write(index, d):
    把 d 写进 URAM 的对应行          # 真实存储
    # 维护深度为 _NCache 的前递队列（_state/_index）
    for i in [_NCache-1 .. 1]: _state[i] = _state[i-1]; _index[i] = _index[i-1]
    _state[0] = d; _index[0] = index          # 最新值永远在 [0]

read(index):
    for i in [0 .. _NCache-1]:                # 先查前递队列
        if index == _index[i]: return _state[i]
    return URAM[index]                        # 未命中才读 URAM
```

关键有两点：前递队列的深度 `_NCache` 必须 ≥ URAM 实际写延迟（故注释建议「在初次综合后再定」）；并且必须**显式告诉 HLS 忽略 `blocks` 上的迭代间依赖**，否则前递逻辑白写——HLS 仍会按保守依赖排流水。这件事在头文件的注释里讲得很明白，并且把 `blocks` 成员特意设为 `public`，就是为了让你能对它写 `#pragma HLS DEPENDENCE variable=blocks inter false`。

至于 URAM 块数怎么算：`need_num` 这个模板元在编译期根据元素位宽 `_WData` 和数组深度 `_NData` 算出需要多少块。逻辑是「一行 72 位能塞几个元素」（`elem_per_line = 72/_WData`），窄元素就多个打包进同一行省块数；超过 72 位则多块并排保证一周期取完。

#### 4.1.3 源码精读

类定义与存储绑定在构造函数里，把 `blocks` 钉死为 URAM 实现并完全展开前两维（让所有块并行寻址）：

[utils/L1/include/xf_utils_hw/uram_array.hpp:89-97](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/uram_array.hpp#L89-L97) — 构造函数里 `bind_storage ... impl = URAM` 决定用 URAM，`array_partition complete dim=1/dim=2` 把 `blocks` 两个维度完全展开以并行访问。

编译期计算 URAM 用量的元函数 `need_num`：

[utils/L1/include/xf_utils_hw/uram_array.hpp:39-54](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/uram_array.hpp#L39-L54) — `value_x` 是需要多少个 4096 深的行（按 `_NData` 上取整），`value_y` 是需要多少个 72 位列（按 `_WData` 上取整）；元素 ≤72 位时一行打包多个（`elem_per_line`），否则每元素占多列。

前递寄存器成员（命中即跳过 URAM 读）：

[utils/L1/include/xf_utils_hw/uram_array.hpp:157-161](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/uram_array.hpp#L157-L161) — `_index[_NCache]` 存最近写入的地址，`_state[_NCache]` 存对应值，深度 `_NCache` 由模板参数决定。

`read()` 先查前递缓存、未命中才落 URAM：

[utils/L1/include/xf_utils_hw/uram_array.hpp:279-285](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/uram_array.hpp#L279-L285) — `Read_Cache` 循环逐项比对 `_index[i]`，命中就 `return _state[i]`，从而绕开 URAM 读延迟。

`write()` 写完 URAM 后移位刷新前递队列：

[utils/L1/include/xf_utils_hw/uram_array.hpp:263-269](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/uram_array.hpp#L263-L269) — `Write_Cache` 把队列整体下移一格，把最新写入塞到 `[0]`，保证最新值总是最先被命中。

头文件里强调的「必须告诉 HLS 忽略依赖」：

[utils/L1/include/xf_utils_hw/uram_array.hpp:73-75](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/uram_array.hpp#L73-L75) — 注释明确：要让前递缓存真正生效，需要在调用处用 pragma 指示 HLS 忽略 `blocks` 的迭代间依赖，并指引读者去看本模块的测试用例。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（不需要上板，读代码即可）。

1. **实践目标**：理解前递缓存与「忽略依赖」pragma 如何共同维持 II=1。
2. **操作步骤**：打开 [utils/L1/tests/uram_array/dut.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/uram_array/dut.cpp)，定位三个循环：`l_read_after_write_test`（第 24–34 行）、`l_update_value_with_1_II`（第 37–44 行）、`l_dump_value`（第 46–51 行）。注意前两个循环里都写了两条 pragma：`#pragma HLS PIPELINE II = 1` 与 `#pragma HLS DEPENDENCE variable = uram_array1.blocks inter false`。
3. **需要观察的现象**：第一个循环里偶数迭代 `write(i, i)`、奇数迭代 `read(i-1)`——这是教科书式的 RAW：刚写的下一拍就读。如果没有前递缓存 + 忽略依赖，HLS 会让 II ≥ URAM 写延迟；有了它们，理论 II=1。
4. **预期结果**：在你脑海中或本地 csynth 后确认 `dut` 的 II=1。注意头文件第 36 行的提醒：`test case requires WData > 36, otherwise cosim will fail`——位宽太小时综合工具的依赖模型会不一样，cosim 可能失败。
5. **延伸**：把两处 `#pragma HLS DEPENDENCE ... inter false` 注释掉再跑 csynth（待本地验证），观察 II 是否被顶高、`dut_csynth.rpt` 里资源是否变化。

#### 4.1.5 小练习与答案

**练习 1**：`UramArray` 的模板参数 `_NCache` 设大设小各有什么影响？

**参考答案**：`_NCache` 是前递队列深度。设太小（< URAM 实际写延迟）→ 命中不到刚写的值，仍触发 RAW，II 上不去；设太大 → 多余的寄存器浪费面积（FF/LUT），但不影响正确性。注释建议在初次综合拿到写延迟后再定。

**练习 2**：为什么 `blocks` 成员被刻意设成 `public`？

**参考答案**：因为用户需要在**调用处**对 `blocks` 写 `#pragma HLS DEPENDENCE variable = uram_array1.blocks inter false`；HLS 的 `DEPENDENCE` pragma 要求变量可见。设成 private 就无法在类外引用它。

---

### 4.2 cache：DDR/HBM 的只读行缓存

#### 4.2.1 概念说明

很多内核要**随机访问**一片 DDR/HBM 数据（地址不连续、不可预测）。DDR/HBM 的随机访问延迟高、带宽利用率低（每次突发只取回一个有用元素）。`cache` 类就是一片「**把最近从 DDR/HBM 取回的整行（512 位）缓存在片上**」的结构：命中就直接从片上返回，未命中才去片外取，从而把随机访问的片外往返次数压到最低。

它与 `UramArray` 的区别很关键：

- `UramArray` 是**可写**的片上大表，前递缓存用来打断写后读依赖；
- `cache` 是**只读**的片外缓冲镜像，片上存的是「DDR 某地址是否已缓存 + 缓存的值」。

`cache` 还支持「**双缓冲**」：一个地址流同时索引两片 DDR（`ddrMem0`/`ddrMem1`），一次未命中同时取回两片的数据——这正是 4.4 节双 DDR 带宽翻倍的基础。

#### 4.2.2 核心流程

`cache::readOnly` 每拍处理一个地址 `index`，流程如下（以带 end flag 的单缓冲版为例）：

```
把 index 拆成 (k00, k01, k10, k11, k20, k30):
    k01 = 512位行号;  k00 = 行内元素号;  (k20,k10) = 片上 RAM 坐标;  k30 = DDR 段号

查 valid 位图 + 地址表（先查深度为 4 的旁路队列，未命中查片上 valid/onChipAddr）:
    if valid[k20][k10] 命中 且 onChipAddr[k20][k10] == k30:
        tmpV = onChipRam0[k20][k10]（命中，不访问 DDR）
    else:
        tmpV = ddrMem[k01]          # 未命中，真正去 DDR 取整行
        把 tmpV 写回 onChipRam0 并更新 valid/onChipAddr

输出 dataStrm.write(tmpV 中对应 k00 的那一段)
```

为了在「读 valid/onChipAddr → 判定 → 又写 valid/onChipAddr」的同地址读写里维持 II=1，`cache` 用了两招：

1. 一组深度为 4 的**旁路队列**（`pingQue`/`addrQue`/`validQue` 等，逻辑同 `UramArray` 的前递寄存器），吸收片上 RAM 的读延迟；
2. `#pragma HLS DEPENDENCE variable = X type = inter direction = RAW distance = 1 true`——注意这里跟 4.1 相反，写的是 `true`：它告诉 HLS「这里确实存在距离为 1 的 RAW 依赖，请按真实情况建模」。结合旁路队列，HLS 据此插入正确的前递逻辑而非保守顶高 II。

片上存储类型可配：`validRamType`/`addrRamType`/`dataRamType` 三选 `0=LUTRAM / 1=BRAM / 2=URAM`。通常 valid 位图浅、访问密 → LUTRAM；数据行深 → URAM。

#### 4.2.3 源码精读

模板参数与三种存储类型的文档：

[utils/L1/include/xf_utils_hw/cache.hpp:27-50](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/cache.hpp#L27-L50) — `T` 是元素类型（不支持 float/double），`ramRow`/`groupRamPart` 决定片上缓存几何，`dataOneLine` 是一个 512 位行装几个元素，`validRamType`/`addrRamType`/`dataRamType` 三档分别选 LUTRAM/BRAM/URAM。

构造函数里按类型 `bind_storage`：

[utils/L1/include/xf_utils_hw/cache.hpp:60-85](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/cache.hpp#L60-L85) — 三个 `if/else` 把 `valid`、`onChipAddr`、`onChipRam0/onChipRam1` 分别绑定到 LUTRAM/BRAM/URAM。

单缓冲 vs 双缓冲的初始化：

[utils/L1/include/xf_utils_hw/cache.hpp:117-145](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/cache.hpp#L117-L145) — `initSingleOffChip` 只清 `onChipRam0`；`initDualOffChip` 额外清 `onChipRam1`，因为双缓冲要缓存两片 DDR。

深度为 4 的旁路队列与 `DEPENDENCE` pragma（单缓冲版）：

[utils/L1/include/xf_utils_hw/cache.hpp:168-188](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/cache.hpp#L168-L188) — `pingQue[4]`/`addrQue[4]`/`validQue[4]` 等全部 `array_partition complete`，配合 `pipeline II=1` 与三行 `DEPENDENCE ... RAW distance=1 true` 维持每拍一拍的查表-更新节奏。

双缓冲版的 `readOnly` 重载（一次地址、两片 DDR、两路输出）：

[utils/L1/include/xf_utils_hw/cache.hpp:561-709](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/cache.hpp#L561-L709) — 签名带 `ddrMem0`/`ddrMem1` 与 `data0Strm`/`data1Strm`；未命中时同时 `tmpV = ddrMem0[k01]; tmpC = ddrMem1[k01];`，并各自维护一套 `pingQue`/`cntQue` 旁路队列。

#### 4.2.4 代码实践

1. **实践目标**：跟踪一次「未命中 → 命中」的地址转换，看清 k00/k01/k10/k20/k30 五个分量的含义。
2. **操作步骤**：在 [utils/L1/include/xf_utils_hw/cache.hpp:191-198](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/cache.hpp#L191-L198) 处，对一个 `index`（元素级地址）按取模/除法拆成五级：`k00`（行内元素号）、`k01`（512 位行号）、`k10/k20`（片上 RAM 坐标）、`k30`（DDR 段号）。再对照第 228 行的命中判定 `if ((validBool == 1) && (address == k30))`。
3. **需要观察的现象**：命中路径只读片上 `onChipRam0`；未命中路径（第 240–262 行）才会 `ddrMem[k01]` 访问 DDR，并顺手把这一行写进片上缓存与四个旁路队列。
4. **预期结果**：你能用一句话说清「为什么一个 512 位行的所有元素共用一份 valid/onChipAddr」——因为 valid/地址是按「行」粒度缓存，dataOneLine 个元素共享同一行的命中状态。
5. **延伸**：把 `valid` 改用 LUTRAM、`onChipRam0` 改用 URAM（即模板末三参从 `1,1,1` 改成 `0,2,2`），说明这样分配的理由（valid 浅→LUTRAM，数据深→URAM）。

#### 4.2.5 小练习与答案

**练习 1**：`cache` 里 `DEPENDENCE` 写的是 `... RAW distance=1 true`，而 `UramArray` 测试里写的是 `inter false`，为什么一个 `true` 一个 `false`？

**参考答案**：`UramArray` 调用者明确知道「前递寄存器已经覆盖了真实依赖」，故用 `false` 让 HLS 完全忽略、放手排 II=1。`cache` 内部的 valid/addr 表确实存在「同地址先读后写」的真实 RAW，故建模为 `true`，由 `cache` 自己的旁路队列（`pingQue` 等）正确处理，HLS 据此生成前递逻辑而非保守顶高 II。两者都是为 II=1 服务，只是「依赖是否真实存在」不同。

**练习 2**：为什么 `cache` 不支持 `float`/`double` 作为 `T`？

**参考答案**：缓存按 512 位行做位级切片（`tmpV.range(size*(k00+1)-1, size*k00)`），依赖 `ap_uint` 的固定位宽与切片语义；浮点类型的位操作在 HLS 里不便直接这样切片，且文档（第 33 行）已声明不支持，需先用整型或定点。

---

### 4.3 DDR/HBM 多 bank 分区与带宽

#### 4.3.1 概念说明

算得再快，数据喂不进来也白搭。一块加速卡上往往有多片物理存储：Alveo 卡有多片 DDR（如 U280 的 HBM 有 32 个通道、外加 DDR），Versal 有 LPDDR/DDR。如果所有内核端口都挤进同一片存储，它们就**串行**排队访问那一条通道，带宽被卡死；如果把不同端口绑到不同 bank，它们就能**并行**访问，聚合带宽近似随均衡使用的 bank 数线性增长。

在 Vitis 流程里，「端口绑到哪片 DDR/HBM」由两处协同决定：

- **链接期**：`system.cfg` 的 `sp=<实例>.<端口>:<bank>` 把内核实例的 m_axi 端口绑到指定 bank；
- **运行期**：主机用 `xrt::bo` 创建缓冲时传 `kernel.group_id(arg)`，让缓冲落在该参数端口可访问的 bank 上（见 u4-l3）。

二者必须一致：`system.cfg` 把端口绑到 bank0，主机就要把对应缓冲的 group_id 设成该端口对应的组号，否则数据流不过去。

#### 4.3.2 核心流程

带宽的直觉公式（承接 u12-l1）：

\[
\text{有效带宽} \approx \min\left(\; \text{bank 数} \times \text{单 bank 峰值带宽},\ \text{内核消耗速率} \;\right) \times \text{命中率}
\]

提升带宽的三条 lever：

1. **多 bank 分区**：把端口散到多片 DDR/HBM，bank 数↑ → 峰值带宽↑。
2. **行缓存**（4.2 的 `cache`）：命中率↑ → 实际片外访问↓，等效带宽↑。
3. **加宽 beat / datawidth**：单端口每拍数据量↑（u12-l1 已讲）。

注意：`sp=` 只声明「端口—bank」映射；端口数受器件引脚/资源限制，HBM 通道多但每通道带宽相对低，DDR 通道少但单通道带宽高，选型要权衡。

#### 4.3.3 源码精读

vss 示例是一个**单 bank 反例**——读端口与写端口都绑到同一条 LPDDR：

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L14) — `sp=mm2s.mem:LPDDR` 把 mm2s（灌数据）的存储端口绑到 LPDDR。

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:33](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L33) — `sp=s2mm.mem:LPDDR` 把 s2mm（收数据）也绑到同一条 LPDDR。两者共享单通道，无法并行——这是 u4-l3 提到的「单 bank 反例」。要提速，典型做法是把 s2mm 改绑到另一片（如 `sp=s2mm.mem:LPDDR2` 或具体 bank 名），让读/写分走两条通道。

内核实例化声明（决定有几个端口可绑 bank）：

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:10-11](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L10-L11) — `nk = mm2s_wrapper:1:mm2s` 与 `nk = s2mm_wrapper:1:s2mm` 各实例化一份，左边的 `mm2s`/`s2mm` 就是 `sp=`/`sc=` 里用的实例名。

PL↔AIE 边界的 AXI Stream 连接（与 bank 无关，是流连接，仅作对比）：

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:23-31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L23-L31) — `sc = mm2s.sig_o_0:vss_fft_ifft_1d_front_transpose.sig_i_0` 等把 mm2s 的 4 路流接到 AIE 转置内核；这是无地址的流连接，不走 bank。

#### 4.3.4 代码实践

1. **实践目标**：学会读 `sp=` 一行，把端口映射到 bank，并理解它与主机 `group_id` 的对应。
2. **操作步骤**：打开上面的 `system.cfg`，找到 `sp=mm2s.mem:LPDDR` 与 `sp=s2mm.mem:LPDDR`。设想把 s2mm 改绑到第二片 LPDDR（改 bank 名），再回到 [dsp/L2/examples/vss_fft_ifft_1d/host.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp)（见 u4-l2/u4-l3）确认 s2mm 输出缓冲的 `group_id` 也应指向改后的端口。
3. **需要观察的现象**：当前 mm2s 与 s2mm 同绑 `LPDDR`，意味着读写在同一条通道上时分复用；若改成分绑两片，理论上读写可并发。
4. **预期结果**：能说清「`sp=` 是链接期声明、`group_id` 是运行期匹配」这条链路；二者下标/名字不一致时数据无法流通。
5. **待本地验证**：在真实多 bank 平台上把 s2mm 改绑另一片 DDR 后跑 hw_emu，观察吞吐是否提升（受平台实际通道数制约）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 4 个 m_axi 端口都绑到同一片 DDR，聚合带宽会变成多少？

**参考答案**：不会变成 4 倍。它们共享同一条物理通道，时分复用排队，聚合带宽≈单 bank 峰值（甚至因仲裁开销略降）。要 4 倍带宽必须分绑到 4 个不同 bank。

**练习 2**：`sp=` 与 `sc=` 在 `system.cfg` 里各管什么？

**参考答案**：`sp=`（streaming port / storage port）把内核的 m_axi 存储端口绑到某片 DDR/HBM bank，是有地址的存储连接；`sc=`（stream connection）把两个 AXI Stream 端口直连，是无地址的流连接（如 PL↔AIE），不涉及 bank。

---

### 4.4 多 DDR 用例对比与报告瓶颈定位

#### 4.4.1 概念说明

本节把前三个模块落地：用 `utils` 库自带的两个只读缓存用例 `cache_ro_1DDR_with_e`（单 DDR）与 `cache_ro_2DDR_with_e`（双 DDR），看「加一片 DDR + 换 URAM」如何同时提升带宽与缓存容量；再讲怎么读综合/实现报告定位瓶颈。

两个用例的测试台都用同一种访问模式：前 1000 次随机地址（制造未命中、压测 DDR），后 1000 次重复同一地址（制造命中、压测片上缓存）。这恰好同时考察「DDR 带宽」与「缓存命中率」两条线。

#### 4.4.2 核心流程

两个用例的差异可以用一张表讲清（均来自各自 `cache_tb.hpp` 与 `cache.cpp`）：

| 维度 | cache_ro_1DDR_with_e | cache_ro_2DDR_with_e |
| --- | --- | --- |
| 缓存行数 `RAMROW` | 1024 | 4096（4 倍容量） |
| `groupRamPart` | 4 | 4 |
| 每行元素数 `EACHLINE` | 512/64 = 8 | 8 |
| 片上存储类型（末三参） | `1,1,1`（全 BRAM） | `2,2,2`（全 URAM） |
| DDR 端口数（m_axi bundle） | 1（`gmem0_0`） | 2（`gmem0_0` + `gmem0_1`） |
| 初始化方法 | `initSingleOffChip()` | `initDualOffChip()` |
| `readOnly` 重载 | 单缓冲（1 DDR → 1 data 流） | 双缓冲（2 DDR → 2 data 流） |
| vivado_syn 内存限额 | 16384 MB | 32768 MB（翻倍） |

带宽提升来自两条叠加的 lever：

1. **双 m_axi 端口**：双缓冲 `readOnly` 一次地址同时索引两片 DDR（`ddrMem0`/`ddrMem1`），未命中时两条通道并发取数 → 聚合读带宽近似翻倍。
2. **URAM 替 BRAM**：`dataRamType` 从 1（BRAM）换 2（URAM），单块密度更高，缓存行数 1024→4096，命中率↑ → 片外往返↓，等效带宽再升一档；同时省下 BRAM 给别的内核用。

注意双缓冲的语义约束（头文件第 405–407 行注释）：两片缓冲**必须以完全相同的地址序列访问**，因为只有一个 `addrStrm` 驱动两次查表。测试台里把同一指针 `ddrMem0` 传了两遍（`syn_top(..., ddrMem0, ddrMem0)`），所以 `data0==data1`，用来验证双端口缓存逻辑自洽。

至于报告定位：utils 顶层 README 明确——这套流程产出的报告「给出逻辑利用率、时序性能、延迟与吞吐的指示」，相关文件在 `test.prj` 下。

#### 4.4.3 源码精读

单 DDR 顶层：单个 m_axi 端口 + BRAM 缓存：

[utils/L1/tests/cache_ro_1DDR_with_e/cache.cpp:28-37](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/cache_ro_1DDR_with_e/cache.cpp#L28-L37) — 一个 `m_axi ... bundle = gmem0_0` 端口；模板末三参 `1,1,1` 全 BRAM；调用 `initSingleOffChip()` 与单缓冲 `readOnly`。

双 DDR 顶层：两个 m_axi 端口 + URAM 缓存：

[utils/L1/tests/cache_ro_2DDR_with_e/cache.cpp:31-43](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/cache_ro_2DDR_with_e/cache.cpp#L31-L43) — 两个 `m_axi` 端口（`gmem0_0`/`gmem0_1`）分别接 `ddrMem0`/`ddrMem1`；模板末三参 `2,2,2` 全 URAM；调用 `initDualOffChip()` 与双缓冲 `readOnly`。

缓存几何差异（行数）：

[utils/L1/tests/cache_ro_1DDR_with_e/cache_tb.hpp:25](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/cache_ro_1DDR_with_e/cache_tb.hpp#L25) — `#define RAMROW (1024)`。

[utils/L1/tests/cache_ro_2DDR_with_e/cache_tb.hpp:25](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/cache_ro_2DDR_with_e/cache_tb.hpp#L25) — `#define RAMROW (4096)`，4 倍于单 DDR 版，依赖 URAM 的高密度。

内存限额差异（反映双缓冲资源更重）：

[utils/L1/tests/cache_ro_1DDR_with_e/description.json:42-43](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/cache_ro_1DDR_with_e/description.json#L42-L43) — `vivado_syn` 限额 `16384` MB。

[utils/L1/tests/cache_ro_2DDR_with_e/description.json:42-43](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/cache_ro_2DDR_with_e/description.json#L42-L43) — `vivado_syn` 限额 `32768` MB，翻倍，对应更大的 URAM 缓存与双端口综合开销。

关于报告产出，utils 官方说明：

[utils/README.md:92-94](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md#L92-L94) — 报告给出逻辑利用率、时序、延迟、吞吐；产物在 `test.prj` 路径下。

vss 的 `[vivado]` 段还演示了「让 Vivado 报告纳入 AIE 资源」与「开物理优化以收敛时序」：

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:39-45](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L39-L45) — `phys_opt_design` 与 `post_route_phys_opt_design` 两步启用（时序收敛），`param=project.enableUnifiedAIEFlow=true` 让 Vivado 的资源报告把 AIE 部分也统计进来。

#### 4.4.4 代码实践

这是本讲**核心实践任务**，对照两个用例说明双 DDR 如何提升只读缓存带宽。

1. **实践目标**：定量理解「双端口 + URAM」相对「单端口 + BRAM」的带宽与容量收益。
2. **操作步骤**：
   - 并排打开 [cache_ro_1DDR_with_e/cache.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/cache_ro_1DDR_with_e/cache.cpp) 与 [cache_ro_2DDR_with_e/cache.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/cache_ro_2DDR_with_e/cache.cpp)，数 `m_axi` 端口数（1 vs 2）与模板末三参（`1,1,1` vs `2,2,2`）。
   - 看 `cache_tb.hpp` 的 `RAMROW`（1024 vs 4096）。
   - 看 `cache_tb.cpp` 的访问序列：两者都是「1000 随机 + 1000 重复同址」，且双 DDR 版把同一指针传两次（第 48 行 `syn_top(..., ddrMem0, ddrMem0)`）。
3. **需要观察的现象**：随机段（前 1000）触发未命中→压测 DDR 带宽，双端口版可并发取两片；重复段（后 1000）几乎全命中→压测片上缓存，URAM 4 倍容量使命中更稳。
4. **预期结果**：你能复述「双 DDR 提升带宽 = 双 m_axi 端口并发读 + URAM 行缓存容量 4 倍带来更高命中率」这两个 lever，并指出代价是片上 URAM/FF 资源与综合内存翻倍。
5. **待本地验证**：在 vck190 上分别 `make run TARGET=csim` 跑两个用例（platform_allowlist 均为 `vck190`），两者都应打印 `PASS`；csynth 后对比 `syn_top_csynth.rpt` 的 BRAM/URAM/FF 与 II。

#### 4.4.5 小练习与答案

**练习 1**：双 DDR 用例为什么把 `ddrMem0` 传两遍（`ddrMem0, ddrMem0`），而不是两片不同数据？

**参考答案**：这是功能自检。双缓冲 `readOnly` 的语义是「同一地址序列索引两片缓冲」，测试台传同一指针使 `data0==data1==ref`，便于一眼判定双端口缓存逻辑是否正确。真实用法里两片可以是不同数据（如两张查找表）。

**练习 2**：为什么双 DDR 版把存储类型从 BRAM 换成 URAM（`1,1,1`→`2,2,2`），而单 DDR 版用 BRAM？

**参考答案**：双 DDR 版要把 `RAMROW` 从 1024 扩到 4096 以提高命中率，URAM 单块密度（4096×72）远高于 BRAM，用 URAM 才能在合理面积内容下 4 倍缓存；单 DDR 版缓存小（1024 行），BRAM 灵活、够用。这是「容量需求驱动存储类型选型」的典型例子。

**练习 3**：综合报告里看到某个循环 II=3，可能的原因有哪些？

**参考答案**：常见三类——(1) 存储端口竞争（多个访问争同一未充分分区的 BRAM/URAM 端口，需 `array_partition`）；(2) 真实迭代间 RAW 依赖未用前递缓存/`DEPENDENCE` 打断（如本讲 `UramArray` 场景）；(3) 资源限额（如 DSP/乘法器不足迫使时分复用）。定位时先看报告里该循环的「dependency / resource / port」告警。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「为随机访问内核设计存储子系统」的纸上设计：

**场景**：你有一个 PL 内核，需要对一片 8 MB 的 DDR 查找表做随机只读访问，且访问局部性较强（热点行集中）。目标平台是 vck190。

**任务**：

1. **选片上缓存**：决定用 `cache` 还是裸 `UramArray`。提示——只读随机访问选 `cache`（行缓存），可写大表选 `UramArray`。说明理由。
2. **定存储类型**：参照 4.4，给 `validRamType`/`addrRamType`/`dataRamType` 各选 LUTRAM/BRAM/URAM 之一，并说明「valid 浅→LUTRAM、数据深→URAM」的取舍。
3. **定 bank 策略**：如果你的内核有两个 m_axi 端口，仿照 4.3 写两条 `sp=`（分别绑两片 DDR），并指出主机侧两个 `xrt::bo` 的 `group_id` 应如何与端口对齐。如果只有一个端口，写出单 bank 的 `sp=` 并指出带宽瓶颈。
4. **读报告**：跑 csynth 后，打开 `test.prj` 下的 `<top>_csynth.rpt`，记录该内核的 II、latency 与 BRAM/URAM/FF/LUT 估计；若 II>1，按 4.4.5 练习 3 的三类原因自查。
5. **预期产物**：一份一页纸方案，含「缓存选型 + 存储类型 + sp= 两行 + 报告关键字段截图/记录」。本任务为设计型，无需真正综合（待本地验证）。

> 参考骨架（示例代码，非项目原有）：
> ```cpp
> // 仿 cache_ro_2DDR 的顶层骨架
> void syn_top(hls::stream<ap_uint<32>>& addrStrm, hls::stream<bool>& e_addrStrm,
>              hls::stream<ap_uint<64>>& data0Strm, hls::stream<bool>& e_data0Strm,
>              hls::stream<ap_uint<64>>& data1Strm, hls::stream<bool>& e_data1Strm,
>              ap_uint<512>* ddrMem0, ap_uint<512>* ddrMem1) {
> #pragma HLS INTERFACE m_axi bundle = gmem0_0 port = ddrMem0
> #pragma HLS INTERFACE m_axi bundle = gmem0_1 port = ddrMem1
>     xf::common::utils_hw::cache<ap_uint<64>, 4096, 4, 8, 32, 0, 1, 2> dut; // valid=LUTRAM, addr=BRAM, data=URAM
>     dut.initDualOffChip();
>     dut.readOnly(ddrMem0, ddrMem1, addrStrm, e_addrStrm,
>                  data0Strm, e_data0Strm, data1Strm, e_data1Strm);
> }
> ```

## 6. 本讲小结

- **UramArray** 用前递寄存器（`_index`/`_state`，深度 `_NCache`）记录最近写入，读命中即跳过 URAM，从而在可写大表上维持 II=1；调用处必须配 `#pragma HLS DEPENDENCE variable=blocks inter false` 才能生效。
- **cache** 是 DDR/HBM 的只读行缓存，按 512 位行缓存「valid + 地址 + 数据」，用深度为 4 的旁路队列与 `DEPENDENCE ... RAW distance=1 true` 维持 II=1；存储类型三档可选（LUTRAM/BRAM/URAM）。
- **DDR/HBM 分区**：`system.cfg` 的 `sp=实例.端口:bank` 在链接期把端口绑到 bank，主机 `xrt::bo` 的 `group_id` 在运行期与之对齐；端口散到多片 bank 才能并发，聚合带宽近似随均衡使用的 bank 数线性增长。vss 示例是单 bank 反例（mm2s/s2mm 同绑 LPDDR）。
- **多 DDR 用例对比**：`cache_ro_2DDR_with_e` 用双 m_axi 端口（并发读、带宽翻倍）+ URAM（`RAMROW` 1024→4096、命中率↑），代价是综合内存限额翻倍。
- **报告瓶颈定位**：csynth 报告看 II/latency/资源（位于 `test.prj`，文件名以 top 函数命名）；vivado_impl 报告看时序；II>1 多源于端口竞争、未打断的 RAW、或资源限额三类，`[vivado]` 段的物理优化开关用于收敛时序。
- **核心权衡**：URAM 密度高但端口少、灵活性低；BRAM 灵活但容量小——选型由「容量需求 vs 灵活性需求」驱动，本讲双 DDR 用例即「容量驱动换 URAM」的实例。

## 7. 下一步学习建议

- **AIE 编程模型深入**（u13-l1）：本讲的 `cache`/`UramArray` 是 PL 侧存储原语；下一步进入 ADF 图，看 PL↔AIE 边界如何由 mm2s/s2mm 与 PLIO/GMIO 桥接，理解「片外 DDR → PL 搬运 → AIE 阵列」的完整数据通路。
- **AIE 图主机控制与 SD 卡打包**（u13-l2）：把本讲的 `system.cfg` `[vivado]` 段与 `v++ --package` 串起来，看时序/资源如何在 hw 构建与上板流程里收口。
- **继续阅读源码**：精读 [utils/L1/include/xf_utils_hw/cache.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/cache.hpp) 的双缓冲 `readOnly` 重载，对照 [data_mover/L1/include/xf_data_mover/pl_4d_data_mover.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_4d_data_mover.hpp) 看更复杂的 URAM 缓存 + 双控制器结构如何吸收 DDR 抖动（u5-l2 已铺垫）。
