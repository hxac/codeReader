# 跨层契约测试（Python↔Verilog↔C）

## 1. 本讲目标

AERIS-10 雷达的一条命令从 Python GUI 发出，要经过 USB 被 Verilog 解码、写进 `host_*` 寄存器，再由 STM32 经 GPIO 读取——三层代码用三种语言写成，却必须对「同一个契约」达成一致。本讲要回答的核心问题是：

> 当一个 bug **同时存在于两层、且两层错得一样**时，普通的「A 对比 B」测试会双双通过，谁也抓不到它。怎么办？

学完本讲你应该能够：

1. 说清 `test_cross_layer_contract.py` 里**三层验证**（静态契约解析 / iverilog 协同仿真 / C stub 执行）各自用什么工具、能抓哪类错误。
2. 理解为什么需要一份**独立推导的真值**（ground truth），而不只是「两层是否一致」。
3. 解释 `contract_parser.py` 如何用正则从 Python / Verilog / C 三套源码里各自「发现」它们实际实现的契约。
4. 看懂「Python 打包 → 真实硬件路径代码运行 → Python 重新解析」这条**回环**如何端到端证明数据不丢位、不截断。

本讲属于测试体系（U11）的第三篇，承接 u11-l1（FPGA 回归与 cosim）与 u11-l2（STM32 shim/mock 测试），并把视野从「单层内部正确」提升到「跨层契约一致」。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（对应前置讲义）：

- **主机命令协议**（u6-l2）：4 字节命令 `{opcode, addr, value}`、26 字节状态包（6 个 32 位字）、opcode 是 Python `Opcode` 枚举与 Verilog `case(usb_cmd_opcode)` 之间的硬契约。
- **STM32 main 与外设**（u7-l1）：STM32 经 GPIO（如 DIG_5/DIG_6）与 FPGA 握手；`RadarSettings` 按位置型协议拆解主机下发的参数包。
- **FPGA 回归与 cosim**（u11-l1）：iverilog 可在无 Vivado 许可证时仿真 Verilog，并用「真实数据 exact-match」做黄金比对。

几个本讲会用到的术语，先统一口径：

- **契约（contract）**：跨层共用的接口约定，例如「opcode `0x23` 写 `host_cfar_alpha` 的低 8 位」「状态包 word 0 的 `radar_mode` 位于 bit[23:22]」。
- **真值 / ground truth**：一份独立于任何一层源码、由人从权威出处（寄存器声明、复位块、数据手册）转录的「正确答案」。
- **回环（round-trip）**：构造输入 → 跑真实代码 → 重新解析输出 → 与原输入比对，证明信息无损。
- **正则解析（regex parsing）**：用 Python `re` 从源码文本里提取结构化事实（不执行源码）。

## 3. 本讲源码地图

本讲涉及的文件全部位于 `9_Firmware/tests/cross_layer/`，外加它们读取的三层目标源码：

| 文件 | 角色 |
| --- | --- |
| [`test_cross_layer_contract.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py) | 唯一的 pytest 编排器，把三层验证组织成若干 `TestTier*` 类。 |
| [`contract_parser.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/contract_parser.py) | 「真值发现器」：用正则从 Python/Verilog/C 源码里各自推导出它们实现的契约。 |
| [`tb_cross_layer_ft2232h.v`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/tb_cross_layer_ft2232h.v) | 第二层的 iverilog testbench，把抓到的字节 dump 成文本文件。 |
| [`adar1000_vm_reference.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/adar1000_vm_reference.py) | 从 ADAR1000 数据手册独立转录的 128 项 I/Q 矢量调制器查找表真值。 |
| `stm32_settings_stub.cpp` | 第三层的 C++ 桩，包裹真实的 `RadarSettings::parseFromUSB` 并打印解析结果。 |
| 被测目标 | `radar_protocol.py`（Python）、`radar_system_top.v` + `usb_data_interface_ft2232h.v`（Verilog）、`RadarSettings.cpp` + `main.cpp` + `ADAR1000_AGC.cpp`（C/C++）。 |

---

## 4. 核心概念与源码讲解

### 4.1 三层验证：静态契约 / 协同仿真 / C stub 执行

#### 4.1.1 概念说明

跨层契约测试要解决的根本难题，写在测试文件的顶部 docstring 里：

> The goal is to find UNKNOWN bugs by testing each layer against **independently-derived ground truth** — not just checking that two layers agree (because both could be wrong).
> （目标是用「独立推导出的真值」去考验每一层，去找未知的 bug——而不是只检查两层是否一致，因为两层可能一起错。）

如果只做「Python 对比 Verilog」，当两层犯同一个错（例如都把 `radar_mode` 放错了 bit 位，或都多写了一个不存在的 opcode）时，对比测试会通过，bug 被完美隐藏。

为了打破这种「共谋」，`test_cross_layer_contract.py` 用**三条相互正交的验证路径**去逼近同一个真值。每条路径的失败模式不同，于是「能骗过一条的 bug，往往骗不过另一条」：

| 层级 | 名称 | 工具 / 语言 | 它把什么当作「真」 | 主要能抓的错 |
| --- | --- | --- | --- | --- |
| Tier 1 | 静态契约解析 | Python `re` 正则 | 人手转录的 `GROUND_TRUTH_*` 常量 | opcode 幻影 / 位宽不符 / 包常量漂移 / 状态字拼接不是 32 位 |
| Tier 2 | 协同仿真 | iverilog + vvp + Python 解析器 | **运行**出来的真实字节流 | 「打包—传输—解析」回环中丢位、错位、字节序错误 |
| Tier 3 | C stub 执行 | `c++` 编译并运行真 `RadarSettings` | 真实可执行代码的输出 | 设置包字段顺序/偏移/类型错误、越界读、坏标记漏拒 |

源码顶部对这三层的精炼定义见 [`test_cross_layer_contract.py:1-25`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L1-L25)。

#### 4.1.2 核心流程

整个文件是一个 pytest 编排器，把测试组织成与三层对应的类，调度方式如下：

```text
                 ┌─────────────────────────────────────────────┐
                 │   contract_parser.py  （真值发现器）          │
                 │   从 Python / Verilog / C 源码各自 regex 出   │
                 │   opcode、位宽、复位默认、包常量、状态字拼接…  │
                 └──────────────────────┬──────────────────────┘
                                        │ 发现的「每层事实」
                 ┌──────────────────────▼──────────────────────┐
   Tier 1 静态    │  GROUND_TRUTH_OPCODES / RESET_DEFAULTS /    │
   契约解析       │  PACKET_CONSTANTS  （人手转录的独立真值）      │
                 │  对比三层事实 == 独立真值？                    │
                 └──────────────────────┬──────────────────────┘
                                        │
   Tier 2 cosim   │  iverilog 编译并运行 tb_cross_layer_ft2232h.v │
                 │  → cmd_results.txt / data_packet.txt /        │
                 │    status_packet.txt                          │
                 │  → 用真 RadarProtocol.parse_* 重新解析回环     │
                 └──────────────────────┬──────────────────────┘
                                        │
   Tier 3 C stub  │  c++ 编译 stm32_settings_stub.cpp + 真         │
                 │  RadarSettings.cpp，Python 构造二进制设置包     │
                 │  → 运行 → 读 stdout → 逐字段比对                │
                 └─────────────────────────────────────────────┘
```

三个工具的可得性在加载期就被探测，并且**在 CI 里缺失工具会被当作硬失败而非静默跳过**：

```python
IVERILOG = os.environ.get("IVERILOG", "iverilog")
VVP = os.environ.get("VVP", "vvp")
CXX = os.environ.get("CXX", "c++")
...
_in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
if _in_ci:
    if not _has_iverilog:
        raise RuntimeError("iverilog is required in CI but was not found. ...")
    if not _has_cxx:
        raise RuntimeError("C++ compiler is required in CI but was not found. ...")
```

这段逻辑见 [`test_cross_layer_contract.py:54-78`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L54-L78)。其设计意图很明确：跨层测试只有在三条路径都真正运行起来时才有意义，所以本地缺工具可以 `skipif`，但 CI 绝不允许悄悄退化成「只跑静态层」。

对应的三个 pytest 类分别是：

- `TestTier1OpcodeContract` / `TestTier1BitWidths` / `TestTier1StatusWordTruncation` 等（静态）— [`test_cross_layer_contract.py:237`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L237) 起。
- `TestTier2VerilogCosim`（协同仿真，带 `@pytest.mark.skipif(not _has_iverilog)`）— [`test_cross_layer_contract.py:943-944`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L943-L944)。
- `TestTier3CStub`（C stub，带 `skipif(not _has_cxx)`）— [`test_cross_layer_contract.py:1126-1127`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L1126-L1127)。

> 小提示：源码里还有一个 `TestTier2Adar1000VmTableGroundTruth` 类（固件 vs 数据手册的 128 项 I/Q 表逐字节比对）。它在编号上属于 Tier 2，但本质是「独立真值」思想的一个极端干净的实例，我们放到 4.2 节细讲。

#### 4.1.3 源码精读

**Tier 1 的判定方式——双向集合比对。** 静态层最关键的一条测试不是「A 是否 ⊆ B」，而是双向都查：

```python
def test_python_verilog_bidirectional_match(self):
    """Python and Verilog must have the same set of opcode values."""
    py_set = set(cp.parse_python_opcodes().keys())
    v_set = set(cp.parse_verilog_opcodes().keys())
    py_only = py_set - v_set
    v_only = v_set - py_set
    assert not py_only, f"Opcodes in Python but not Verilog: {[hex(x) for x in py_only]}"
    assert not v_only, f"Opcodes in Verilog but not Python: {[hex(x) for x in v_only]}"
```

见 [`test_cross_layer_contract.py:273-280`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L273-L280)。注意它只证明了「Python 与 Verilog 一致」——这正是我们刚刚说的「两层一致不代表正确」的缺口。真正堵住缺口的，是下面这条把人手转录真值当作唯一仲裁者的测试：

```python
def test_python_opcodes_match_ground_truth(self):
    py_opcodes = cp.parse_python_opcodes()
    for val, entry in py_opcodes.items():
        assert val in GROUND_TRUTH_OPCODES, (
            f"Python Opcode {entry.name}=0x{val:02X} not in ground truth! "
            f"Possible phantom opcode (like the 0x06 incident)."
        )
```

见 [`test_cross_layer_contract.py:240-247`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L240-L247)。

**Tier 2 的 fixture——编译一次、跑一次、读三个文件。** cosim 用一个 `scope="class"` 的 fixture 把昂贵的「编译 + 仿真」只做一次，再把产物（`cmd_results.txt` / `data_packet.txt` / `status_packet.txt` 的文本内容）分发给本类所有用例：

```python
@pytest.fixture(scope="class")
def tb_results(self, tmp_path_factory):
    workdir = tmp_path_factory.mktemp("verilog_cosim")
    tb_path = THIS_DIR / "tb_cross_layer_ft2232h.v"
    rtl_path = cp.FPGA_DIR / "usb_data_interface_ft2232h.v"
    out_bin = workdir / "tb_cross_layer_ft2232h"
    # 编译：testbench + 被测 USB 接口模块
    result = subprocess.run(
        [IVERILOG, "-o", str(out_bin), "-I", str(cp.FPGA_DIR),
         str(tb_path), str(rtl_path)], ...)
    # 运行
    result = subprocess.run([VVP, str(out_bin)], ..., cwd=str(workdir))
    # 把 TB dump 的文本读回来
    return {"stdout": result.stdout,
            "cmd_results": (workdir / "cmd_results.txt").read_text(),
            "data_packet": (workdir / "data_packet.txt").read_text(),
            "status_packet": (workdir / "status_packet.txt").read_text()}
```

见 [`test_cross_layer_contract.py:947-978`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L947-L978)。注意它编译时**只带 `usb_data_interface_ft2232h.v` 一个 RTL 文件**——这是刻意收窄的边界：Tier 2 在这一层只验证「USB 接口的打包/解包」，不去拉起整个 `radar_system_top`（那是 u11-l1 全链路回归的职责）。

**Tier 3 的 fixture——把真 `RadarSettings.cpp` 编进主机可执行文件。** C stub 这一层的关键，是把目标机的嵌入式代码搬到 PC 上编译运行：

```python
@pytest.fixture(scope="class")
def stub_binary(self, tmp_path_factory):
    stub_src = THIS_DIR / "stm32_settings_stub.cpp"
    radar_settings_src = cp.MCU_LIB_DIR / "RadarSettings.cpp"
    out_bin = workdir / "stm32_settings_stub"
    result = subprocess.run(
        [CXX, "-std=c++11", "-o", str(out_bin),
         str(stub_src), str(radar_settings_src),
         f"-I{cp.MCU_LIB_DIR}"], ...)
```

见 [`test_cross_layer_contract.py:1130-1145`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L1130-L1145)。注意它与 u11-l2 的 shim/mock 路线**不同**：u11-l2 用假 HAL 把 main.cpp 的逻辑抽出来测；而这里 Tier 3 故意链接**真实的** `RadarSettings.cpp`，因为这一层要验证的就是「真实解析代码」的字段顺序与二进制布局，不能被桩替换掉。

#### 4.1.4 代码实践

**实践目标**：亲手确认三层验证真的在三条独立路径上跑，而不是「一个测试换个名字跑三遍」。

**操作步骤**：

1. 确认本机有 `iverilog` 与 `c++`（CI 之外的本地环境若缺，相应 Tier 会 `skipif`）。
2. 在仓库根目录运行：
   ```bash
   uv run pytest 9_Firmware/tests/cross_layer/test_cross_layer_contract.py -v
   ```
3. 在输出里找到三类用例的前缀：`TestTier1...`、`TestTier2VerilogCosim...`、`TestTier3CStub...`，记录各自通过 / 跳过 / 失败的数量。
4. 临时把 `IVERILOG` 指向一个不存在的路径再跑一次：
   ```bash
   IVERILOG=/nope/iverilog uv run pytest 9_Firmware/tests/cross_layer/test_cross_layer_contract.py -v
   ```
   观察 Tier 2 是否整体变成 `SKIPPED`（本地非 CI 行为）。

**需要观察的现象**：每个 Tier 收集到的用例数应明显不同（Tier 1 最多，因为它是逐契约展开的断言集合）；Tier 2/3 在缺工具时**静默跳过**，但失败信息会提示工具缺失。

**预期结果**：三层各自独立运行；缺工具时跳过而非报错。**待本地验证**（取决于本机是否装了 iverilog/build-essential）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Tier 2 的 `tb_results` fixture 要用 `scope="class"` 而不是默认的 `scope="function"`？

> **答案**：iverilog 编译 + vvp 仿真相对昂贵（秒级）。`scope="class"` 让同类的多个用例共享一次编译/运行结果，只把已 dump 的文本读回来反复解析；若用 function 级，每个用例都要重新编译仿真，浪费时间且可能因临时目录抖动引入不稳定。

**练习 2**：如果把 `usb_data_interface.v`（FT601 版）也一起编进 Tier 2 的 iverilog 命令，会发生什么？

> **答案**：会出现重复模块定义（两个文件都定义了模块的某些同名辅助逻辑，或顶层选择冲突）。当前命令刻意只带 `usb_data_interface_ft2232h.v` 一个 RTL，就是为了避免 FT601/FT2232H 双版本的符号冲突；FT601 的等价检查由 Tier 1 的 `test_status_words_concat_widths_ft601` 用静态正则完成（见 [`test_cross_layer_contract.py:347-364`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L347-L364)）。

---

### 4.2 独立真值推导：contract_parser 与 GROUND_TRUTH

#### 4.2.1 概念说明

4.1 节反复强调「独立真值」，本节回答两个问题：**真值从哪里来？每层的事实又怎么得到？**

关键设计哲学写在 `contract_parser.py` 的顶部：

> These parsers do NOT define the expected values — they **discover what each layer actually implements**, so the test can compare layers against ground truth and find bugs where both sides are wrong (like the 0x06 phantom opcode or the status_words[0] 37-bit truncation).
> （这些解析器不定义期望值——它们「发现」每层实际实现了什么，于是测试可以拿每层去和真值比，从而抓出「两边一起错」的 bug，例如 0x06 幻影 opcode、或 status_words[0] 的 37 位截断。）

见 [`contract_parser.py:11-15`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/contract_parser.py#L11-L15)。

这里有两类「真」，必须严格区分：

1. **每层事实（per-layer facts）**：由 `contract_parser.py` 用正则从源码里**发现**——它不评判对错，只如实记录「Python 的 Opcode 枚举里有这些值」「Verilog 的 case 语句写了这些寄存器」。
2. **独立真值（ground truth）**：由人从权威出处**转录**到 `test_cross_layer_contract.py` 顶部的 `GROUND_TRUTH_*` 字典里——它的权威来自寄存器声明、复位块、数据手册，而不是任何一层源码。

测试的断言形式恒为 `每层事实 == 独立真值`。两层之间互相比较只是「附带」的健全性检查（`test_python_verilog_bidirectional_match`），不是仲裁者。

#### 4.2.2 核心流程

独立真值有三张表，分别覆盖「命令」「复位默认值」「包常量」：

```python
GROUND_TRUTH_OPCODES = {
    0x01: ("host_radar_mode", 2),
    0x02: ("host_trigger_pulse", 1),   # pulse
    0x03: ("host_detect_threshold", 16),
    ...
    0x23: ("host_cfar_alpha", 8),
    ...
    0x30: ("host_self_test_trigger", 1),  # pulse
    0x31: ("host_status_request", 1),     # pulse
    0xFF: ("host_status_request", 1),     # alias, pulse
}
GROUND_TRUTH_RESET_DEFAULTS = {"host_radar_mode": 1, "host_detect_threshold": 10000,
                               "host_cfar_alpha": 0x30, "host_agc_enable": 0, ...}
GROUND_TRUTH_PACKET_CONSTANTS = {
    "data":   {"header": 0xAA, "footer": 0x55, "size": 11},
    "status": {"header": 0xBB, "footer": 0x55, "size": 26},
}
```

见 [`test_cross_layer_contract.py:171-230`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L171-L230)。它的注释明确标注自己是「manually transcribed from radar_system_top.v」的**单一真源**（single source of truth）。

而「每层事实」由 `contract_parser.py` 提供，函数命名遵循 `parse_<层>_<契约>` 的规律：

| 解析函数 | 读取的源 | 发现的事实 |
| --- | --- | --- |
| `parse_python_opcodes` | `radar_protocol.py` | Opcode 枚举 `{值: 名字}` |
| `parse_verilog_opcodes` | `radar_system_top.v` | case 语句 `{值: 寄存器/位切片/是否脉冲}` |
| `parse_verilog_register_widths` | `radar_system_top.v` | `reg [W-1:0] host_*` 声明的位宽 |
| `parse_verilog_reset_defaults` | `radar_system_top.v` | 复位块里 `host_* <= N'dVal` 的默认值 |
| `parse_verilog_packet_constants` | `usb_data_interface_ft2232h.v` | `localparam HEADER/FOOTER/STATUS_HEADER/*_PKT_LEN` |
| `parse_python_status_fields` | `radar_protocol.py` | `parse_status_packet` 的位移/掩码 → 位域 |
| `parse_verilog_status_word_concats` + `count_concat_bits` | `usb_data_interface_ft2232h.v` | `status_words[N] <= {...}` 拼接的总位数 |
| `parse_stm32_settings_fields` | `RadarSettings.cpp` | 设置包字段顺序/偏移/类型 |

#### 4.2.3 源码精读

**「发现」而非「定义」——以 Verilog opcode 解析为例。** 看它如何从 `case(usb_cmd_opcode)` 里抽出事实：

```python
# Pattern 1: Simple assignment — 8'hXX: register <= rhs;
for m in re.finditer(
    r"8'h([0-9a-fA-F]{2})\s*:\s*(\w+)\s*<=\s*(.*?)(?:;|$)",
    text, re.MULTILINE
):
    value = int(m.group(1), 16)
    register = m.group(2)
    rhs = m.group(3).strip()
    is_pulse = rhs in ("1", "1'b1")          # 触发类：写常量 1
    slice_m = re.search(r'usb_cmd_value(\[\d+(?::\d+)?\])', rhs)  # 位切片
    ...
```

见 [`contract_parser.py:304-333`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/contract_parser.py#L304-L333)。注意三件事：它**同时**识别出「写到哪个寄存器」「取 `cmd_value` 的哪几位」「是不是自清零脉冲」——这些就是 4.1 节断言所需要的全部事实。对应的真实 Verilog 长这样（[`radar_system_top.v:950-988`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L950-L988)）：

```verilog
case (usb_cmd_opcode)
    8'h01: host_radar_mode     <= usb_cmd_value[1:0];
    8'h03: host_detect_threshold <= usb_cmd_value;
    8'h23: host_cfar_alpha         <= usb_cmd_value[7:0];
    8'h28: host_agc_enable         <= usb_cmd_value[0];
    8'h30: host_self_test_trigger  <= 1'b1;   // 自清零脉冲
    ...
```

而 `host_cfar_alpha` 的位宽声明 `reg [7:0] host_cfar_alpha;` 在 [`radar_system_top.v:265`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L265)，由 `parse_verilog_register_widths` 用另一条正则独立发现——于是「case 里切的位宽」与「寄存器声明位宽」可以**交叉验证**。

**算拼接位数——抓 37 位截断。** `count_concat_bits` 把 Verilog 的 `{...}` 拼接表达式逐段加总：

```python
def count_concat_bits(concat_expr: str, port_widths: dict[str, int]) -> ConcatWidth:
    inner = concat_expr.strip().strip("{}")
    total = 0
    for part in re.split(r',\s*', inner):
        lit_match = re.match(r"(\d+)'[bdhoBDHO]", part)   # 字面量 N'bXXX
        if lit_match: total += int(lit_match.group(1)); continue
        sel_match = re.match(r'(\w+)\[(\d+):(\d+)\]', part)  # 带位选信号
        if sel_match: total += int(sel_match.group(2)) - int(sel_match.group(3)) + 1; continue
        if part in port_widths: total += port_widths[part]    # 裸信号查端口宽
        ...
    return ConcatWidth(total_bits=total, target_bits=32, truncated=total > 32 ...)
```

见 [`contract_parser.py:470-525`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/contract_parser.py#L470-L525)。它喂的是 `usb_data_interface_ft2232h.v` 里 word 0 的真实拼接（[`usb_data_interface_ft2232h.v:376-378`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L376-L378)）：

```verilog
// Word 0: {0xFF[31:24], mode[23:22], stream[21:19], 3'b000[18:16], threshold[15:0]}
status_words[0] <= {8'hFF, status_radar_mode, status_stream_ctrl,
                    3'b000, status_cfar_threshold};
```

把每段位宽相加：

\[
\underbrace{8}_{\texttt{8'hFF}} + \underbrace{2}_{\texttt{status\_radar\_mode}} + \underbrace{3}_{\texttt{status\_stream\_ctrl}} + \underbrace{3}_{\texttt{3'b000}} + \underbrace{16}_{\texttt{status\_cfar\_threshold}} = 32
\]

现在它正好 32 位。而历史上那个 bug 是中间的保留段写成了更宽的字面量，使总和达到 37，Verilog 静默截掉高位 5 位。测试 `test_status_words_concat_widths_ft2232h` 就是把 `total_bits == 32` 当硬断言（[`test_cross_layer_contract.py:328-345`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L328-L345)）。

> **为什么「两层一致但都错」需要第三层独立真值——以 0x06 幻影 opcode 为例。**
> 假设开发者在 Python 的 `Opcode` 枚举和 Verilog 的 `case` 里**同时**遗留了一个 opcode `0x06`（两边都定义、语义一致，但没有对应寄存器、也不在规格里）。
> - `test_python_verilog_bidirectional_match` 会**通过**——因为 Python 集合 == Verilog 集合。
> - 但 `0x06` 是个「幻影」：FPGA 收到它什么都不该做，GUI 却以为它在控制某个功能。
> - `test_python_opcodes_match_ground_truth` 会**失败**——因为人手转录的 `GROUND_TRUTH_OPCODES`（来自寄存器声明/复位块）里没有 `0x06`，错误信息直指「Possible phantom opcode (like the 0x06 incident)」。
>
> 这就是「独立真值」的价值：它的权威不来自任何一层源码，所以两层一起错时它仍能戳穿。

**跨层不变量——AGC enable 必须三层一致。** `TestTier1AgcCrossLayerInvariant` 把同一个信号 `host_agc_enable` 在三层中的传播当成一条系统级不变量来查：

- FPGA：`assign gpio_dig6 = host_agc_enable;`（不能被接地）— [`radar_system_top.v:1044`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L1044)；复位默认为 0（AGC 上电关闭）— [`radar_system_top.v:938`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L938)。
- MCU：`ADAR1000_AGC` 构造函数必须 `enabled(false)`；主循环必须 `HAL_GPIO_ReadPin(FPGA_DIG6 ...)` 读 DIG_6 同步外环，且要带「两帧确认」去抖。
- GUI：必须从状态包 `words[4] >> 11` 解析 AGC 位。

见整个类 [`test_cross_layer_contract.py:446-625`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L446-L625)。它甚至会断言 MCU 端有 `static bool dig6_prev = false;` 且 `outerAgc.enabled = dig6_now;` 必须包在 `if (dig6_now == dig6_prev)` 里（[`test_cross_layer_contract.py:562-625`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L562-L625)）——这是把「PR 描述里声称做了去抖」变成可执行断言的典型手法。

**最干净的独立真值样例——ADAR1000 矢量调制器查找表。** `adar1000_vm_reference.py` 把数据手册 Rev. B Tables 13-16 的 128 项 (I,Q) 字节**逐个转录**成 `GROUND_TRUTH`，并辅以 ADI Linux beamformer 驱动作为第二来源交叉核对（[`adar1000_vm_reference.py:9-31`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/adar1000_vm_reference.py#L9-L31)）。测试再把固件 `ADAR1000_Manager.cpp` 里的 `VM_I[128]` / `VM_Q[128]` 抽出来逐字节比对：

```python
firmware = adar_vm.parse_array(cpp_source, "VM_I")
mismatches = [(k, firmware[k], gt[k][2]) for k in range(128) if firmware[k] != gt[k][2]]
assert not mismatches, f"VM_I diverges from datasheet at {len(mismatches)} indices..."
```

见 [`test_cross_layer_contract.py:833-853`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L833-L853)。它还内建了**自检**：转录表的角度必须落在均匀 2.8125° 网格上、象限对称、四个基本点（0/90/180/270°）与手册极值完全一致（[`adar1000_vm_reference.py:108-178`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/adar1000_vm_reference.py#L108-L178)）——独立真值本身也要可证伪，否则「真值抄错了」会变成新 bug 源。

更有意思的是 `test_adversarial_corruption_is_detected`：它故意构造一个把 `VM_I[42]` 翻转 1 位的「坏固件」片段，确认解析器**能**检出差异（[`test_cross_layer_contract.py:900-936`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L900-L936)）。这是「测试测试本身」——防止将来有人把比对改成只比 `len()` 而让整套契约测试形同虚设。

#### 4.2.4 代码实践

**实践目标**：亲手制造一个「两层一致但都错」的 bug，看独立真值如何抓到它。

**操作步骤**：

1. 阅读三条断言的依赖关系：
   - `test_python_opcodes_match_ground_truth`（[`test_cross_layer_contract.py:240`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L240)）
   - `test_verilog_opcodes_match_ground_truth`（[`test_cross_layer_contract.py:257`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L257)）
   - `test_python_verilog_bidirectional_match`（[`test_cross_layer_contract.py:273`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L273)）
2. 在 `radar_protocol.py` 的 `Opcode` 枚举里**临时**加一行 `PHANTOM = 0x06`（**不要提交，仅供实验**）。
3. 运行：
   ```bash
   uv run pytest 9_Firmware/tests/cross_layer/test_cross_layer_contract.py -k "opcode" -v
   ```
4. 观察哪些用例 FAIL、哪些仍 PASS。

**需要观察的现象**：`test_python_verilog_bidirectional_match` 会因为「Python 多了 0x06 而 Verilog 没有」而 FAIL——但如果你**同时在 Verilog 的 case 里也补一个 `8'h06:` 分支**，这条双向比对就会**变绿通过**；唯有 `test_python_opcodes_match_ground_truth` 仍然 FAIL，并提示「Possible phantom opcode」。实验结束务必把两处临时改动还原（不要污染源码）。

**预期结果**：证明了「两层一致」可以被轻易绕过，而独立真值不能。**待本地验证**（结果取决于你能否在不影响其他测试的前提下干净地加这条实验分支）。

#### 4.2.5 小练习与答案

**练习 1**：`count_concat_bits` 遇到拼接里出现一个「裸信号」（既不是字面量、也不带位选），它如何决定该信号的位宽？

> **答案**：查 `port_widths` 字典——该字典由 `get_usb_interface_port_widths` 从 USB 接口模块的端口声明（如 `input wire [15:0] status_cfar_threshold`）正则解析而来。如果裸信号不在字典里，函数返回 `total_bits = -1`（表示「无法计算」），调用方 `test_status_words_concat_widths_*` 据此 `pytest.skip` 而不是误判通过。

**练习 2**：为什么 ADAR1000 的真值表要内置 `check_uniform_2p8125_deg_step`、`check_quadrant_symmetry`、`check_cardinal_points` 三项自检？

> **答案**：因为「真值」是人从数据手册逐字节抄进来的，抄错（行错位、极性位翻反）本身就是高概率 bug。这三项自检用数学结构（均匀网格、180° 对称、四个基本点）约束转录结果，让「真值错」能在比对固件之前就先暴露——否则错真值会让正确的固件反而被判失败，方向完全反了。

---

### 4.3 回环正确性：Verilog cosim 与 C stub 端到端

#### 4.3.1 概念说明

4.2 节的静态层有一个盲区：**正则只能看到文本，看不到运行时行为**。例如它无法发现「USB 读 FSM 在某个时序边沿采样错了字节」「toggle-CDC 跨时钟域丢了脉冲」「`extractDouble` 在边界长度上越界读」。这些只有把代码**真正跑起来**才会暴露。

于是有了「回环（round-trip）」这一思想：用已知 distinctive 值构造输入 → 跑真实代码 → 用另一层的真实解析器重新解读输出 → 与原输入逐字段比对。如果回环闭合，就端到端证明了「打包—传输—解析」链无损。

两条回环：

1. **命令/数据/状态回环（Tier 2）**：Verilog TB 把字节 dump 成文本 → Python 用真 `RadarProtocol.parse_*` 解析。
2. **设置包回环（Tier 3）**：Python `struct.pack` 构造二进制包 → 真实 `RadarSettings::parseFromUSB` 解析 → stdout 比对。

#### 4.3.2 核心流程

**Tier 2 的三道练习**，由 `tb_cross_layer_ft2232h.v` 顶部 docstring 明确定义（[`tb_cross_layer_ft2232h.v:3-24`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/tb_cross_layer_ft2232h.v#L3-L24)）：

```text
Exercise A 命令回环（Host→FPGA）：
  每个 opcode 走一遍 4 字节读 FSM → dump opcode/addr/value → cmd_results.txt
Exercise B 数据包生成（FPGA→Host）：
  注入 range/doppler/cfar 已知值 → 抓 11 字节 → data_packet.txt
Exercise C 状态包生成（FPGA→Host）：
  把所有状态输入设为已知非零值 → 触发 status_request → 抓 26 字节 → status_packet.txt
```

TB 内部用「distinctive values」让截断/交换 bug 一眼可见——例如 `range_profile = 0xCAFE_BEEF`、`doppler_real=0x1234 / doppler_imag=0x5678`（[`tb_cross_layer_ft2232h.v:575-577`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/tb_cross_layer_ft2232h.v#L575-L577)）。这样若高低字节被换位，`0xCA` 跑到了 LSB 位置，立刻穿帮。

**Tier 3 的回环**则由 `_build_settings_packet` 按真实二进制布局打包：

```python
def _build_settings_packet(self, values: dict) -> bytes:
    pkt = b"SET"
    for key in ["system_frequency", "chirp_duration_1", "chirp_duration_2"]:
        pkt += struct.pack(">d", values[key])       # 3 个大端 double
    pkt += struct.pack(">I", values["chirps_per_position"])  # 1 个大端 uint32
    for key in ["freq_min", "freq_max", "prf1", "prf2", "max_distance", "map_size"]:
        pkt += struct.pack(">d", values[key])       # 6 个大端 double
    pkt += b"END"
    return pkt
```

见 [`test_cross_layer_contract.py:1147-1161`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L1147-L1161)。这个「`SET` + 9×double + 1×uint32 + `END` = 82 字节」正是 `RadarSettings.cpp` 期望的布局——两端必须对同一份字节流达成共识。

#### 4.3.3 源码精读

**为什么 TB 要用 `fork...join` 并行抓字节？** Exercise B 里有一段关键注释解释了时序陷阱：

```verilog
// CRITICAL: Must capture bytes IN PARALLEL with the trigger,
// because the write FSM starts sending bytes ~3-4 ft_clk cycles
// after the toggle CDC propagates. If we wait for CDC propagation
// first, capture_write_bytes misses the early bytes.
fork
    assert_range_valid(32'hCAFE_BEEF);
    capture_write_bytes(11);
join
```

见 [`tb_cross_layer_ft2232h.v:584-591`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/tb_cross_layer_ft2232h.v#L584-L591)。这是因为 `range_valid` 从 100 MHz 域跨到 60 MHz 的 `ft_clk` 域要经过 toggle-CDC（3 级同步 + 边沿检测，4 拍以上），而写 FSM 在 CDC 传播完成前几个 `ft_clk` 就已经开始吐字节。如果串行「先触发、等 CDC、再抓」，开头几个字节就丢了——这正是回环测试要抓的那类「时序导致丢字节」的真实 bug，TB 自己也得避免犯同样的错。

`send_command_ft2232h` 则逐拍模拟 FT2232H 读 FSM 的 8 个状态（RD_IDLE → OE_ASSERT → READING×4 → DEASSERT → PROCESS），注释里把每一拍采样哪个字节、何时切换 `host_data_drive` 写得一清二楚（[`tb_cross_layer_ft2232h.v:213-276`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/tb_cross_layer_ft2232h.v#L213-L276)）。

**Python 回读——把抓到的 11 字节喂给真解析器。** 这是回环的「另一侧」：

```python
rows = _parse_hex_results(tb_results["data_packet"])
raw = bytes(int(row[1], 16) for row in rows)   # 把 TB dump 的 hex 重组成字节
parsed = RadarProtocol.parse_data_packet(raw)   # 用 GUI 真解析器解读
...
assert parsed["range_q"] == (0xCAFE - 0x10000)   # 有符号化
assert parsed["doppler_i"] == 0x1234
assert parsed["detection"] == 1
```

见 [`test_cross_layer_contract.py:1004-1045`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L1004-L1045)。注意它验证的是「Verilog 打包的字节流，经 Python 解析后还原成注入值」——这是一条横跨两种语言的真实数据通路，不是文本比对。

状态包回读同理，且专门盯 AGC 字段（word 4）与 `radar_mode`/`stream_ctrl`（word 0）的位域（[`test_cross_layer_contract.py:1047-1119`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L1047-L1119)），与 4.2 节静态层的位域检查互为印证。

**C stub 回环——真实 `parseFromUSB` 编译运行。** 桩程序本身极薄，只做「读文件 → 调真解析 → 打印」：

```cpp
RadarSettings settings;
bool ok = settings.parseFromUSB(buf, (uint32_t)file_size);
...
printf("parse_ok=true\n");
printf("system_frequency=%.17g\n", settings.getSystemFrequency());
printf("chirps_per_position=%u\n", settings.getChirpsPerPosition());
...
```

见 [`stm32_settings_stub.cpp:61-83`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/stm32_settings_stub.cpp#L61-L83)。`%.17g` 用满 double 的可表示精度打印，这样 Python 端用 `abs(actual - expected) < expected * 1e-10` 才能严格比对（[`test_cross_layer_contract.py:1207-1212`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L1207-L1212)）。

Tier 3 还包含**负面回环**——构造坏包确认会被拒绝：

- `test_truncated_packet_rejected`：`b"SET" + b"\x00"*40 + b"END"`（46 字节，不足 82）必须返回 `parse_ok=false`（[`test_cross_layer_contract.py:1244-1250`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L1244-L1250)）。
- `test_bad_markers_rejected`：把 `SET`/`END` 改成 `BAD` 必须被拒（[`test_cross_layer_contract.py:1252-1271`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L1252-L1271)）。

> 补充：静态层 `test_minimum_packet_size` 还**文档化了一个已知 bug**——`RadarSettings.cpp` 里长度校验写的是 74，但实际需要的最小长度是 `3 + (9×8 + 4) + 3 = 82` 字节，差 8 字节意味着按 74 放行会在最后一个 double 上**越界读**（[`test_cross_layer_contract.py:702-731`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L702-L731)）。这种「测试即缺陷登记簿」的用法，让契约测试同时承担了 known-issue 看护职责。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 11 字节数据包从 Verilog 打包到 Python 解析的完整回环，理解「distinctive values」如何让 bug 显形。

**操作步骤**：

1. 打开 [`tb_cross_layer_ft2232h.v:574-628`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/tb_cross_layer_ft2232h.v#L574-L628)，记下注入值：`range_profile=0xCAFE_BEEF`（即 Q=0xCAFE, I=0xBEEF）、`doppler_real=0x1234`、`doppler_imag=0x5678`、`cfar_detection=1`。
2. 对照 TB 的本地断言，确认每个字节位置：byte0=`0xAA`（头）、byte1=`0xCA`、…、byte9=`0x81`（frame_start=1 且 detection=1）、byte10=`0x55`（尾）。
3. 再看 [`test_cross_layer_contract.py:1022-1045`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/test_cross_layer_contract.py#L1022-L1045)，确认 Python 把 `0xCAFE` 当成有符号 16 位解读为 `-13570`（`0xCAFE - 0x10000`）。
4. 思考：如果 USB 接口里把 `range_profile[31:24]` 与 `[23:16]` 两段写反了，TB 的 byte1/byte2 会变成什么？Python 端 `range_q` 会变成什么？

**需要观察的现象**：所有字节都来自「可记忆的十六进制模式」（CAFE/BEEF/1234/5678），任何字节换位都会破坏模式，让 bug 在 dump 文件里肉眼可见——这就是 distinctive values 的设计意图。

**预期结果**：你能凭注入值在脑中算出每个字节，并与 TB 断言、Python 断言两边对上。这一步是纯源码阅读，不需要运行即可确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Tier 3 用 `%.17g` 打印 double，而不是 `printf("%f", ...)`？

> **答案**：`%f` 默认只 6 位小数，会丢掉 double 的有效位，Python 端就无法做 `abs(actual-expected) < expected*1e-10` 的严格比对。`%.17g` 能完整表示 IEEE-754 double 的全部约 17 位有效数字，确保回环「无损」，让任何字节序/对齐错误都反映为数值不一致而非被打印截断掩盖。

**练习 2**：Exercise B 为什么必须在 `fork...join` 里**同时**触发 `assert_range_valid` 和 `capture_write_bytes`，而不是先触发再抓？

> **答案**：`range_valid` 是跨时钟域脉冲，要经 toggle-CDC（3 级同步 + 边沿检测）才能搬到 `ft_clk` 域；而 USB 写 FSM 在 CDC 还没传完时就已经开始发字节。串行「先触发等 CDC、再抓」会错过头几个字节，导致 11 字节抓不全、回环假性失败。并行触发让抓取窗口覆盖整个发送过程，是 TB 自己避免「时序丢字节」这个它正要检测的 bug 类别。

---

## 5. 综合实践

把本讲的三条主线串成一个任务：**给跨层契约测试新增一条 opcode 契约，并让它同时被三层验证覆盖。**

场景：假设要新增一个主机命令「设置 MTI 对消器阶数」，opcode `0x17`，写入一个 3 位寄存器 `host_mti_taps`，复位默认 `3'd3`。请完成以下设计清单（**纸面设计即可，不要真的改源码**）：

1. **独立真值**：在 `GROUND_TRUTH_OPCODES` 里追加 `0x17: ("host_mti_taps", 3)`；在 `GROUND_TRUTH_RESET_DEFAULTS` 里追加 `"host_mti_taps": 3`。说明这两条分别从哪份权威出处（寄存器声明、复位块）转录。
2. **三层事实**：
   - Python：在 `Opcode` 枚举里加 `MTI_TAPS = 0x17`——确认 `parse_python_opcodes` 的正则能自动发现它。
   - Verilog：在 `radar_system_top.v` 声明 `reg [2:0] host_mti_taps;`、复位块加 `host_mti_taps <= 3'd3;`、case 表加 `8'h17: host_mti_taps <= usb_cmd_value[2:0];`——确认 `parse_verilog_opcodes` / `parse_verilog_register_widths` / `parse_verilog_reset_defaults` 三条正则都能各自独立抓到。
   - 指出 Tier 2 的命令回环（`tb_cross_layer_ft2232h.v` Exercise A）需要新增哪条 `send_command_ft2232h(8'h17, ...)` 与对应 `$fwrite`/`check`。
3. **回答本讲核心问题**：说明三层验证各自用什么工具/语言、各能抓哪类错误；并举一个「两层一致但都错」的例子（可借鉴 0x06 幻影 opcode 或 37 位截断），说明为什么缺了第三层独立真值就抓不到。

完成后，你应当能用一句话讲清：**契约测试不是「测两层一致」，而是「用一份独立真值，从静态、仿真、执行三个正交方向去证伪每一层」。**

## 6. 本讲小结

- 跨层契约测试的目标是抓**未知** bug，手段是用「独立推导的真值」考验每一层——而不是只查两层是否一致，因为两层可能一起错。
- 三层验证各正交：**Tier 1** 用 Python 正则做静态契约解析（抓 opcode/位宽/包常量/状态字位数错误）；**Tier 2** 用 iverilog 协同仿真跑真 USB 接口并 dump 字节（抓时序丢字节/字节序错误）；**Tier 3** 用 `c++` 编译运行真 `RadarSettings`（抓二进制布局/越界/坏标记）。
- `contract_parser.py` 只**发现**每层事实、不定义期望值；`GROUND_TRUTH_*` 才是人手转录的仲裁者；`adar1000_vm_reference.py` 是「数据手册真值 + 自检」最干净的范例。
- 回环思想：用 distinctive 值构造输入 → 跑真实代码 → 用另一层真解析器还原 → 逐字段比对，端到端证明无损；负面回环（截断/坏标记）还要确认坏输入被拒。
- 工程细节同样重要：CI 缺工具是硬失败、`scope="class"` fixture 复用仿真、TB 必须用 `fork...join` 避免自己丢字节、测试还能当 known-bug 登记簿（74 vs 82 字节）。

## 7. 下一步学习建议

- **U11 收尾后**：阅读 u11-l4（Python 测试、ruff 与代码质量），把「质量门禁」补齐；至此 FPGA/MCU/Python/跨层四套测试体系就完整了。
- **想深入形式化证明**：进入 u14-l1（形式化验证 SymbiYosys），看 `.sby` 如何对 `radar_mode_controller`、CDC 等模块证明「某些性质恒成立」——它与本讲的「枚举式契约」互补，一个查接口、一个证行为。
- **想动手扩展系统**：直接做本讲第 5 节的综合实践，再参照 u14-l2（二次开发扩展点）把 opcode 同步到顶层 case 表、`radar_protocol.py`、GUI 与测试，体会「新增一个主机命令」要同时改哪些地方才不破坏跨层契约。
- **继续读源码**：精读 [`contract_parser.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/tests/cross_layer/contract_parser.py) 的每一条正则，思考它的失败模式（例如 `count_concat_bits` 遇到嵌套大括号会怎样），这与 `adar1000_vm_reference.py` 里 `parse_array` 注释明示的「`[^}]*` 限制」是同一类有意识的取舍。
