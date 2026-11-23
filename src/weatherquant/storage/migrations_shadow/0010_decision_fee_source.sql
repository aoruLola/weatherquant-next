-- WQN-46: a decision must say where its fee came from.
--
-- NULL keeps the meaning it has everywhere else in this schema: the fee was not
-- established. 'venue' means the market reported it; 'fallback' means the dated
-- schedule supplied it because the venue reported none. Storing the source next
-- to the rate is what stops a fallback assumption from silently reading as a
-- venue fact when the decisions are reviewed later.
ALTER TABLE shadow_decisions ADD COLUMN fee_source VARCHAR;
