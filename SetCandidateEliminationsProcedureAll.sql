CREATE OR ALTER PROCEDURE dbo.ApplySetCandidateEliminations_All
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @BattleId VARCHAR(64);

    DECLARE battle_cursor CURSOR FAST_FORWARD FOR
        SELECT DISTINCT BP.BattleId
        FROM dbo.BattlePokemon BP
        JOIN dbo.SetCandidates SC ON SC.BattlePokemonId = BP.BattlePokemonId
        WHERE BP.BattleId LIKE 'gen9battlefactory-%';

    OPEN battle_cursor;
    FETCH NEXT FROM battle_cursor INTO @BattleId;

    WHILE @@FETCH_STATUS = 0
    BEGIN
        EXEC dbo.ApplySetCandidateEliminations @BattleId = @BattleId;
        FETCH NEXT FROM battle_cursor INTO @BattleId;
    END

    CLOSE battle_cursor;
    DEALLOCATE battle_cursor;
END
GO
