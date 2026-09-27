CREATE OR ALTER PROCEDURE dbo.MarkSuspicious_All
AS
BEGIN
  SET NOCOUNT ON;

  DECLARE @BattleId VARCHAR(64);

  DECLARE cur CURSOR FAST_FORWARD FOR
    SELECT BattleId
    FROM dbo.Battles
    WHERE BattleId LIKE 'gen9battlefactory-%';

  OPEN cur;
  FETCH NEXT FROM cur INTO @BattleId;

  WHILE @@FETCH_STATUS = 0
  BEGIN
    EXEC dbo.MarkSuspiciousMovesForBattle @BattleId = @BattleId;
    EXEC dbo.MarkSuspiciousTeraForBattle  @BattleId = @BattleId;

    FETCH NEXT FROM cur INTO @BattleId;
  END

  CLOSE cur;
  DEALLOCATE cur;
END
GO
