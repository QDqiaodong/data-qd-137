本目录为原创虚构合成数据，使用MIT许可。没有真实个人、线上实验、生产日志、交易、项目进度或搜索用户信息；不来自外部平台。

- input.json：单次 --source/--output 命令的三人两项目样例。
- workbench.json：工作台样例。四申请者、两兄弟项目共享 root 容量 2，演示跨项目
  区域限额；另含退档释放祖先额度后重新竞争的演练数据。
- empty_workbench.json：空申请者、零容量项目与区域的合法边界样例。
- audit_source.json / audit_policy.json：双边声明核查样例。两项目各含一条单边
  声明（C 仅被 X 排序、C 仅志愿 Y），政策只对 (C,X) 提供有效免审；政策文件内嵌
  源文件字节 SHA256，源字节变化即失效。
- materials_roster.json / materials_policy.json：招生材料库样例。四申请者（其中 D
  无志愿）与两项目，沿用 contract.md 的 applicants/programs 严格结构；政策为每个
  项目声明材料 kind（X 要求 id_copy/transcript，Y 要求 id_copy/statement），演示
  同一申请者两个志愿的不同材料要求与空志愿边界。
