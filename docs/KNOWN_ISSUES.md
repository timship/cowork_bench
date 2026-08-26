# Известные проблемы

## Критический гейт не покрывает главный артефакт (58 задач)

**Статус:** подтверждено, не исправлено. Аудит от 2026-08-26 (70 агентов, каждая находка
перепроверена отдельным скептиком; 2 находки отклонены при верификации).

### Механика

Грейдеры засчитывают задачу при `accuracy >= 70%`, а поверх этого работает критический гейт:
провал помеченной проверки = FAIL независимо от процента. Гейт исправен, но проверка
«главный файл существует» в этих задачах **не помечена критичной**, и сразу за ней идёт ранний
`return`. В результате все критичные проверки этого артефакта не выполняются вовсе — они не
проваливаются, а исчезают из знаменателя.

Итог: агент, не создавший главный артефакт, набирает высокий процент на второстепенных
проверках и проходит задачу. Возникает извращённый стимул — **частично неверный файл ведёт к
провалу, а его отсутствие к успеху**.

Пример (`arxiv-method-benchmark-tracker`, проверен исполнением грейдера, exit code 0):

```
check_excel : файла нет -> 1 FAIL -> return; 7 критичных проверок не записаны
check_teamly: 3 PASS
total = 4, accuracy = 75% >= 70  -> PASS, файл Method_Benchmark.xlsx отсутствует
```

### Как чинить

Для каждой задачи — один из двух способов:

1. добавить имя проверки существования в набор критичных (`CRITICAL_CHECKS` / `CRITICAL` /
   `critical=True` — в пуле используются все три написания);
2. либо в ветке раннего возврата явно записать зависимые критичные проверки как проваленные —
   так уже сделано в guard'ах отсутствующих листов у части задач.

Образец исправления — семь задач `rzd-*`, починенных в коммите 74dea824.

### Затронутые задачи

1. `academic-llm-reasoning-ru-arxiv-word-pptx` — LLM_Reasoning_Review.docx (+ LLM_Reasoning_Slides.pptx)
2. `arxiv-latex-reasoning-gsheet` — Google Sheet 'Reasoning Methods Comparison' (sheet 'Papers') + Reasoning_Methods_Review.docx
3. `arxiv-lit-review-gsheet` — Google Sheet spreadsheet (Paper Comparison + Technique Analysis tabs) in gsheet.*
4. `arxiv-method-benchmark-tracker` — Method_Benchmark.xlsx (plus Teamly tracker page)
5. `arxiv-reading-plan-excel-gcal-email` — Reading_Plan.xlsx (Papers + Schedule sheets)
6. `arxiv-research-tracker-teamly` — Transformer_Research_Tracker.xlsx
7. `arxiv-rlhf-conference-prep-ru-pptx-gcal-email` — RLHF_Conference_Report.pptx
8. `canvas-assignment-deadline-word-gcal` — Assignment_Deadlines_FFF2013J.xlsx (+ Assignment_Schedule_FFF2013J.docx)
9. `canvas-assignment-workload-excel` — Workload_Report.xlsx
10. `canvas-exam-prep-scheduler` — Exam_Prep.xlsx
11. `canvas-grade-distribution-word` — Grade_Report.docx (+ Grade_Data.xlsx)
12. `canvas-grade-equity-excel-word-gcal` — Grade_Equity_Analysis.xlsx (+ Equity_Report.docx)
13. `canvas-grades-gsheet-pdf-email` — Grade_Dashboard_Reference.xlsx
14. `canvas-late-submission-gcal` — Late_Submissions.xlsx (sheets 'By Course' / 'Top Offenders')
15. `canvas-quiz-analysis-email` — Quiz_Performance.xlsx (sheet 'Quiz Analysis')
16. `canvas-quiz-analysis-gsheet-email` — Google Sheet "Quiz Performance Tracker" (+ Quiz_Performance_Summary.docx)
17. `canvas-quiz-item-analysis-word-gcal-email` — Quiz_Item_Analysis.xlsx
18. `canvas-quiz-performance-excel` — Quiz_Performance.xlsx
19. `canvas-sf-skills-gap-excel-word-email` — Skills_Gap_Report.xlsx
20. `canvas-ta-workload-excel-email` — TA_Workload_Report.xlsx
21. `clickhouse-hr-salary-benchmark-forms-excel-email` — Salary_Analysis.xlsx (Department_Stats + Summary sheets)
22. `clickhouse-sales-territory-realign-ppt-gsheet-email` — Territory_Analysis.xlsx (+ Territory_Review.pptx)
23. `fetch-canvas-performance-excel-word-email` — Performance_Report.xlsx (+ the .docx report)
24. `fetch-insales-inventory-forecast-excel-gcal-email` — Inventory_Forecast_Report.xlsx (Stock_Status / Supplier_Info / Restock_Summary)
25. `fetch-insales-market-analysis-excel-ppt-email` — Competitive_Analysis.xlsx (+ Strategy_Presentation.pptx)
26. `fetch-insales-supplier-restock-excel-word-gcal` — Restocking_Plan.xlsx
27. `fetch-sf-sales-forecast-ppt-gcal` — Sales_Forecast_Data.xlsx (+ Q2_Sales_Forecast.pptx)
28. `fetch-terminal-data-pipeline` — Data_Pipeline_Report.xlsx (Sales Analysis / Inventory Status / Combined View)
29. `forms-clickhouse-feedback` — Support_Analysis.xlsx (sheets 'Issue Summary' + 'Priority Breakdown')
30. `gcal-clickhouse-deadline-tracker` — SLA_Compliance_Audit.xlsx ('Breached Tickets' + 'Summary' sheets)
31. `insales-category-performance-ppt` — Category_Review.pptx (+ Category_Data.xlsx)
32. `insales-customer-retention-email` — VIP_Customer_Report.xlsx
33. `insales-fetch-supply-chain-excel-word-gcal` — Supply_Chain_Optimization.xlsx (Inventory_Status + Summary sheets)
34. `insales-product-bundle-excel-ppt-gcal` — Bundle_Analysis.xlsx
35. `insales-product-launch-dashboard` — Product_Review.xlsx (Category Performance / Market Comparison / Opportunities) + Q1_Product_Review.pptx
36. `insales-refund-root-cause-excel-word-email` — Refund_Analysis.xlsx (3 sheets) — plus Root_Cause_Report.docx, whose existence check is also unmarked
37. `insales-review-analysis-word-gform` — Review_Analysis.docx
38. `insales-shipping-performance-gsheet-word-email` — Google Sheet "Shipping Performance Dashboard" (co-primary with Shipping_Performance_Report.docx)
39. `insales-vip-customer-gsheet-gcal-email` — Google Sheet 'VIP Customer Tracker'
40. `inventory-supply-chain-optimization` — Inventory_Forecast_Report.xlsx (4 sheets)
41. `kulinar-grocery-budget-planner` — Meal_Budget.xlsx
42. `kulinar-meal-plan-calendar` — Google Sheet with "Меню недели" + "Список покупок" sheets
43. `kulinar-nutrition-benchmark` — Nutrition_Benchmark.xlsx
44. `kulinar-survey-analysis` — Preference_Analysis.xlsx
45. `kulinar-teamly-recipe-kb` — Recipe_Comparison.xlsx ('All Recipes' / 'Top Picks')
46. `kulinar-wellness-excel-gcal-email` — Wellness_Meal_Plan.xlsx
47. `market-competitive-intelligence-report` — Competitive_Analysis.xlsx (Competitors + Pricing_Comparison) and Competitive_Report.docx
48. `moex-earnings-report-word-teamly` — Earnings_Report.docx
49. `moex-market-summary-teamly` — Market_Overview.xlsx
50. `moex-peer-comparison-excel-ppt-email` — Peer_Comparison.xlsx (Company Profiles / Financial Comparison / Scoring) + Investor_Presentation.pptx
51. `moex-portfolio-analysis-excel-word-email` — Portfolio_Analysis.xlsx
52. `moex-portfolio-gsheet-pdf-gcal-email` — Portfolio_Dashboard_Reference.xlsx (Holdings/Performance/Rebalancing) + the 'Portfolio Dashboard' Google Sheet/PDF
53. `moex-stock-comparison-excel-gcal` — Stock_Comparison_Report.xlsx (Price History + Performance Summary)
54. `pw-sf-benchmark-excel-word-email` — Benchmark_Analysis.xlsx
55. `pw-sf-hr-dept-efficiency-excel-gcal` — Hr_Dept_Efficiency_Report.xlsx (Data_Analysis / Metrics / Recommendations)
56. `rzd-clickhouse-sales-trip-excel-word-email` — Sales_Trip_Plan.xlsx (Travel_Details / Customer_Priority / Summary)
57. `scholarly-arxiv-survey-word-teamly-gcal` — LLM_Reasoning_Survey.docx
58. `terminal-canvas-kulinar-gsheet-teamly-email` — Nutrition_Academic_Study.xlsx (Student_Engagement + Meal_Plans)

### Прочее

- **`inert_gate`** — у одной из перечисленных задач критическая структура существует, но не влияет
  на вердикт вовсе.
- Проверка задачи `arxiv-rlhf-conference-prep-ru-pptx-gcal-email` была прервана классификатором
  безопасности; её вердикт в список не включён и требует отдельного разбора.
- Полные разборы с арифметикой и конкретными эксплойтами по каждой задаче остались в отчёте
  аудита и не переносились сюда целиком.

