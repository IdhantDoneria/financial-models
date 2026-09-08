const models = [
  "Discounted Cash Flow","Gordon Growth (DDM)","Modern Portfolio Theory",
  "Value at Risk / CVaR","CAPM","Fama-French 3-Factor","Black-Scholes-Merton",
  "Binomial Tree (CRR)","Monte Carlo (GBM)","Heston Stochastic Volatility",
  "Ind AS 116 Hidden-Debt Normalizer","Reverse DCF / Market-Implied Expectations"];
document.getElementById('scorebody').innerHTML = models.map(m =>
  `<tr><td class="m">${m}</td>
   <td><span class="chip">10</span></td><td><span class="chip">10</span></td>
   <td><span class="chip">10</span></td><td><b>10.0</b></td></tr>`).join('');
